import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.schemas.check_result import CheckResultResponse
from app.schemas.check_result import InspectionResponse
from app.schemas.slow_sql import SlowSQLResponse
from app.services import basic_info as basic_info_service
from app.services import db_control as db_control_service
from app.services import inspection as inspection_service
from app.services.inspection import dispatch as inspection_dispatch
from app.services import slow_sql_analysis as slow_sql_analysis_service
from app.services import instances as instance_service
from app.services import archive_fra as archive_fra_service
from app.services import backup as backup_service
from app.services import parameters as params_service
from app.services import redo_log as redo_log_service
from app.services import table_index as table_index_service
from app.services import slow_sql as slow_sql_service
from app.services import ssh_executor as ssh_service
from app.services import tablespace_enhanced as ts_enhanced
from app.services import capacity_monitor as capacity_service
from app.services import user_mgmt as user_mgmt_service
from app.services import oracle_undo as undo_service
from app.services import oracle_tablespace_cleanup as ts_cleanup_service
from app.schemas.oracle_undo import UndoRetentionPlanRequest
from app.schemas.oracle_index import (
    RebuildIndexPlanRequest,
    CreateIndexPlanRequest,
    CheckDuplicateRequest,
)
from app.schemas.oracle_stats import (
    GatherTableStatsRequest,
    GatherSchemaStatsRequest,
    TableStatsPlanRequest,
)
from app.schemas.oracle_partition import PartitionPlanRequest
from app.services import oracle_index
from app.services import oracle_stats
from app.services import oracle_partition
from app.services import oracle_archive_file
from app.services import oracle_table_usage
from app.services import oracle_dataguard
from app.services import oracle_temp
from app.schemas.oracle_archive_file import (
    CsvPlanRequest,
    ExpdpPlanRequest,
    DirectoryPlanRequest,
    VerifyPlanRequest,
    FullArchiveFilePlanRequest,
)
from app.schemas.oracle_archive_db import ArchiveDbPlanRequest
from app.services import oracle_archive_db
from app.schemas.oracle_tablespace_cleanup import (
    SysauxAwrCleanupPlanRequest,
    SysauxAwrRetentionPlanRequest,
    SystemAuditCleanupPlanRequest,
)
from app.schemas.oracle_table_usage import (
    IndexObjectPlanRequest,
    TableUsageCreateIndexPlanRequest,
    TableUsageIndexAdviceRequest,
)

router = APIRouter(prefix="/oracle", tags=["Oracle 运维中心"])
logger = logging.getLogger("app.api.oracle")


def _oracle_read_degraded(instance: DBInstance, operation: str, exc: Exception, fallback: dict) -> dict:
    message = f"{operation} 暂不可用：{exc}"
    logger.warning(
        "Oracle read degraded | instance_id=%s | operation=%s | error=%s",
        instance.id,
        operation,
        exc,
    )
    data = dict(fallback)
    data["status"] = "degraded"
    data["warnings"] = [message]
    data.setdefault("items", [])
    data.setdefault("total", 0)
    return data


@router.get("/{instance_id}/basic-info")
def oracle_basic_info(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return basic_info_service.get_basic_info(instance)


@router.get("/{instance_id}/tablespace-overview")
def oracle_tablespace_overview(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return ts_enhanced.get_tablespace_overview(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "表空间概览",
            exc,
            {
                "instance_name": instance.instance_name,
                "omf_enabled": False,
                "db_create_file_dest": "",
                "tablespaces": [],
                "summary": {
                    "total_count": 0,
                    "total_gb": 0,
                    "used_gb": 0,
                    "usage_percent": 0,
                    "warning_count": 0,
                    "critical_count": 0,
                },
            },
        )


@router.get("/{instance_id}/tablespace/{tablespace_name}/datafiles")
def oracle_tablespace_datafiles(instance_id: int, tablespace_name: str, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return ts_enhanced.get_tablespace_datafiles(instance, tablespace_name)


@router.post("/{instance_id}/tablespace-expand-script")
def oracle_tablespace_expand_script(
    instance_id: int,
    tablespace_name: str = Query(...),
    datafile_path: str = Query(...),
    file_count: int = Query(1, ge=1, le=20),
    size_mb: int = Query(1024),
    autoextend: bool = Query(True),
    next_mb: int = Query(1024),
    maxsize_mb: int = Query(30720),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return ts_enhanced.generate_expand_script(instance, tablespace_name, datafile_path, file_count, size_mb, autoextend, next_mb, maxsize_mb)


@router.get("/{instance_id}/tablespaces/system-audit/summary")
def oracle_system_audit_summary(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return ts_cleanup_service.get_system_audit_summary(instance)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/tablespaces/system-audit/cleanup-plan")
def oracle_system_audit_cleanup_plan(instance_id: int, payload: SystemAuditCleanupPlanRequest, db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    try:
        return ts_cleanup_service.build_system_audit_cleanup_plan(
            retention_days=payload.retention_days,
            include_fga=payload.include_fga,
            include_location_plan=payload.include_location_plan,
            audit_trail_location=payload.audit_trail_location,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/tablespaces/sysaux-awr/summary")
def oracle_sysaux_awr_summary(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return ts_cleanup_service.get_sysaux_awr_summary(instance)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/tablespaces/sysaux-awr/cleanup-plan")
def oracle_sysaux_awr_cleanup_plan(instance_id: int, payload: SysauxAwrCleanupPlanRequest, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return ts_cleanup_service.build_sysaux_awr_cleanup_plan(instance, payload.retention_days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/tablespaces/sysaux-awr/retention-plan")
def oracle_sysaux_awr_retention_plan(instance_id: int, payload: SysauxAwrRetentionPlanRequest, db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    try:
        return ts_cleanup_service.build_sysaux_awr_retention_plan(payload.retention_days, payload.interval_minutes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/db-status")
def oracle_db_status(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return db_control_service.check_db_status(instance)


@router.get("/{instance_id}/boot-prerequisites")
def oracle_boot_prerequisites(
    instance_id: int,
    deep: bool = Query(False, description="是否执行 Oracle 动态视图深度检查"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return db_control_service.check_boot_prerequisites(instance, deep=deep)


@router.get("/{instance_id}/boot-script")
def oracle_boot_script(
    instance_id: int,
    mode: str = Query("open", pattern="^(nomount|mount|open|dg)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return db_control_service.generate_boot_script(instance, mode)


@router.get("/{instance_id}/startup-script")
def oracle_startup_script(
    instance_id: int,
    mode: str = Query("open", pattern="^(nomount|mount|open)$"),
    pfile_path: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return db_control_service.generate_startup_script(instance, mode, pfile_path)


@router.get("/{instance_id}/shutdown-script")
def oracle_shutdown_script(
    instance_id: int,
    mode: str = Query("immediate", pattern="^(normal|transactional|immediate|abort)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return db_control_service.generate_shutdown_script(instance, mode)


@router.get("/{instance_id}/parameters")
def oracle_parameters(
    instance_id: int,
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return params_service.get_parameters(instance, keyword, page, page_size)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "参数查询",
            exc,
            {"items": [], "total": 0, "page": page, "page_size": page_size},
        )


@router.get("/{instance_id}/parameters/sga")
def oracle_sga_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_sga_parameters(instance)


@router.get("/{instance_id}/parameters/pga")
def oracle_pga_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_pga_parameters(instance)


@router.get("/{instance_id}/parameters/process")
def oracle_process_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_process_parameters(instance)


@router.get("/{instance_id}/parameters/session")
def oracle_session_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_session_parameters(instance)


@router.get("/{instance_id}/password-policy")
def oracle_password_policy(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_password_policy(instance)


@router.get("/{instance_id}/audit-config")
def oracle_audit_config(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_audit_config(instance)


@router.get("/{instance_id}/nls-parameters")
def oracle_nls_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_nls_parameters(instance)


@router.get("/{instance_id}/dblinks")
def oracle_dblinks(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_dblinks(instance)


@router.get("/{instance_id}/parameters/datafile")
def oracle_datafile_params(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_datafile_parameters(instance)


@router.get("/{instance_id}/supplemental-log")
def oracle_supplemental_log(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_supplemental_log(instance)


@router.post("/{instance_id}/ransomware-check")
def oracle_ransomware_check(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.check_ransomware(instance)


@router.get("/{instance_id}/alter-system-script")
def oracle_alter_system_script(
    instance_id: int,
    param_name: str = Query(...),
    value: str = Query(...),
    scope: str = Query("both", pattern="^(memory|spfile|both)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.generate_alter_system_sql(instance, param_name, value, scope)


@router.get("/{instance_id}/redo-log")
def oracle_redo_log(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return redo_log_service.get_redo_logs(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "Redo Log 查询",
            exc,
            {"groups": [], "members": [], "parameters": [], "wait_events": []},
        )


@router.get("/{instance_id}/redo-log/switch-frequency")
def oracle_redo_switch_frequency(
    instance_id: int,
    thread: int | None = Query(None),
    limit: int = Query(50, ge=10, le=500),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return redo_log_service.get_redo_switch_frequency(instance, thread, limit)


@router.get("/{instance_id}/redo-log/add-group-script")
def oracle_redo_add_group(
    instance_id: int,
    size_mb: int = Query(1024, ge=50, le=102400),
    member_path: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return redo_log_service.generate_add_group_script(instance, size_mb, member_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{instance_id}/redo-log/add-member-script")
def oracle_redo_add_member(
    instance_id: int,
    group_num: int = Query(..., ge=1),
    member_path: str = Query(..., min_length=1, max_length=512),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return redo_log_service.generate_add_member_script(instance, group_num, member_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{instance_id}/redo-log/switch-script")
def oracle_redo_switch(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return redo_log_service.generate_switch_log_script(instance)


@router.get("/{instance_id}/redo-log/drop-group-script")
def oracle_redo_drop_group(
    instance_id: int,
    group_num: int = Query(..., ge=1),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return redo_log_service.generate_drop_group_script(instance, group_num)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{instance_id}/locked-objects")
def oracle_locked_objects(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return _get_lock_details(instance)


@router.get("/{instance_id}/blocking-tree")
def oracle_blocking_tree(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return _get_blocking_tree(instance)
    except Exception as e:
        logger.error("blocking-tree 查询失败 | id=%s | error=%s", instance_id, e)
        return {"tree": [], "lock_pairs": [], "blocker_count": 0, "waiter_count": 0, "max_wait_s": 0, "error": str(e)}


@router.get("/{instance_id}/kill-session-script")
def oracle_kill_session_script(
    instance_id: int,
    sid: int = Query(...),
    serial: int = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    sql = f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE;"
    return {
        "status": "plan_only",
        "message": "已生成 Kill Session 脚本。请在维护窗口执行。",
        "sql": sql,
        "warnings": ["Kill Session 会强制终止会话，未提交事务将回滚。", "请确认该会话确实需要终止。"],
    }


@router.get("/{instance_id}/os-kill-script")
def oracle_os_kill_script(
    instance_id: int,
    spid: int = Query(...),
    sid: int | None = Query(None),
    serial: int | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    script = (
        "# Plan Only: OS 级强制终止 Oracle server process\n"
        f"# instance={instance.instance_name} sid={sid or '-'} serial={serial or '-'} spid={spid}\n"
        f"ps -fp {spid}\n"
        f"kill -9 {spid}\n"
        f"ps -fp {spid} || echo 'process {spid} has exited'\n"
    )
    return {
        "status": "plan_only",
        "message": "已生成 OS kill -9 方案。请确认业务影响后由 DBA 在主机侧执行。",
        "sql": script,
        "warnings": [
            "kill -9 会绕过数据库正常会话清理，可能触发长事务回滚和实例后台清理。",
            "执行前请先优先尝试 ALTER SYSTEM KILL SESSION 或 DISCONNECT SESSION。",
            "必须确认 SPID 对应的就是目标会话，避免误杀数据库后台进程。",
        ],
    }


@router.get("/{instance_id}/capacity/summary")
def oracle_capacity_summary(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return capacity_service.latest_summary(db, instance)


@router.get("/{instance_id}/capacity/growth")
def oracle_capacity_growth(
    instance_id: int,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return capacity_service.analyze_capacity(db, instance, days)


@router.post("/{instance_id}/tablespaces/create-script")
def oracle_create_tablespace_script(
    instance_id: int,
    tablespace_name: str = Query(...),
    datafile_path: str = Query(...),
    size_mb: int = Query(1024),
    autoextend: str = Query("on", pattern="^(on|off)$"),
    next_mb: int = Query(1024),
    maxsize_mb: int = Query(30720),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    auto_clause = f"AUTOEXTEND ON NEXT {next_mb}M MAXSIZE {maxsize_mb}M" if autoextend == "on" else "AUTOEXTEND OFF"
    sql = f"CREATE TABLESPACE {tablespace_name}\nDATAFILE '{datafile_path}' SIZE {size_mb}M\n{auto_clause};"
    return {"status": "plan_only", "message": "已生成创建表空间脚本。", "sql": sql, "warnings": ["请确认路径和磁盘空间。", "建议使用 OMF (db_create_file_dest) 自动管理。"]}


@router.post("/{instance_id}/tablespaces/create-temp-script")
def oracle_create_temp_tablespace_script(
    instance_id: int,
    tablespace_name: str = Query(...),
    tempfile_path: str = Query(...),
    size_mb: int = Query(1024),
    autoextend: str = Query("on", pattern="^(on|off)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    auto_clause = f"AUTOEXTEND ON NEXT 1024M MAXSIZE 30720M" if autoextend == "on" else "AUTOEXTEND OFF"
    sql = f"CREATE TEMPORARY TABLESPACE {tablespace_name}\nTEMPFILE '{tempfile_path}' SIZE {size_mb}M\n{auto_clause};"
    return {"status": "plan_only", "message": "已生成创建临时表空间脚本。", "sql": sql, "warnings": ["请确认路径和磁盘空间。"]}


@router.delete("/{instance_id}/tablespaces/{tablespace_name}/drop-script")
def oracle_drop_tablespace_script(
    instance_id: int,
    tablespace_name: str,
    including_contents: bool = Query(False),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    inc = " INCLUDING CONTENTS AND DATAFILES" if including_contents else ""
    sql = f"-- 确认表空间无业务对象后执行:\nDROP TABLESPACE {tablespace_name}{inc};"
    return {"status": "plan_only", "message": "已生成删除表空间脚本。此操作不可逆！", "sql": sql, "warnings": ["此操作不可逆，请确认表空间中无业务数据。", "INCLUDING CONTENTS 会删除所有对象。", "建议先备份。"]}


@router.get("/{instance_id}/omf-status")
def oracle_omf_status(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return params_service.get_datafile_parameters(instance)


@router.get("/{instance_id}/archive-config")
def oracle_archive_config(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return archive_fra_service.get_archive_config(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "归档配置",
            exc,
            {"parameters": [], "destinations": [], "mode": []},
        )


@router.get("/{instance_id}/flashback-status")
def oracle_flashback_status(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return archive_fra_service.get_flashback_status(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "Flashback 状态",
            exc,
            {
                "database": [],
                "parameters": [],
                "fra": [],
                "flashback_usage": [],
                "flashback_space": [],
                "flashback_on": "",
                "fra_used_pct": 0,
                "risk_level": "unknown",
            },
        )


@router.get("/{instance_id}/flashback-plan")
def oracle_flashback_plan(
    instance_id: int,
    action: str = Query("enable", pattern="^(enable|disable|retention|extend_fra)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.generate_flashback_plan(instance, action)


@router.get("/{instance_id}/archive-enable-script")
def oracle_archive_enable(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.generate_archive_enable_script(instance)


@router.get("/{instance_id}/archive-disable-script")
def oracle_archive_disable(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.generate_archive_disable_script(instance)


@router.get("/{instance_id}/archive-path-script")
def oracle_archive_path_script(
    instance_id: int, dest_id: int = Query(1), path: str = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.generate_archive_path_script(instance, dest_id, path)


@router.get("/{instance_id}/fra/status")
def oracle_fra_status(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.get_fra_status(instance)


@router.get("/{instance_id}/backup/exp-script")
def oracle_exp_script(
    instance_id: int, schemas: str = Query(...), directory: str = Query("DATA_PUMP_DIR"),
    dumpfile: str = Query("export.dmp"), parallel: int = Query(4),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_exp_script(instance, schemas, directory, dumpfile, parallel)


@router.get("/{instance_id}/backup/imp-script")
def oracle_imp_script(
    instance_id: int, schemas: str = Query(...), dumpfile: str = Query("export.dmp"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_imp_script(instance, schemas, dumpfile)


@router.get("/{instance_id}/backup/expdp-script")
def oracle_expdp_script(
    instance_id: int, schemas: str = Query(...), directory: str = Query("DATA_PUMP_DIR"),
    dumpfile: str = Query("export.dmp"), parallel: int = Query(4),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_expdp_script(instance, schemas, directory, dumpfile, parallel)


@router.get("/{instance_id}/backup/impdp-script")
def oracle_impdp_script(
    instance_id: int, schemas: str = Query(...), directory: str = Query("DATA_PUMP_DIR"),
    dumpfile: str = Query("export.dmp"), parallel: int = Query(4),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_impdp_script(instance, schemas, directory, dumpfile, parallel)


@router.get("/{instance_id}/backup/rman-backup-script")
def oracle_rman_backup_script(
    instance_id: int, backup_type: str = Query("full", pattern="^(full|incremental)$"),
    destination: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_rman_backup_script(instance, backup_type, destination)


@router.get("/{instance_id}/backup/rman-restore-script")
def oracle_rman_restore_script(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.generate_rman_restore_script(instance)


@router.get("/{instance_id}/backup/rman-history")
def oracle_rman_backup_history(
    instance_id: int,
    days: int = Query(30, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.list_rman_backup_history(instance, days, limit)


@router.get("/{instance_id}/users")
def oracle_users(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return user_mgmt_service.list_users(instance)
    except Exception as exc:
        return _oracle_read_degraded(instance, "用户列表", exc, {"users": []})


@router.get("/{instance_id}/roles")
def oracle_roles(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return user_mgmt_service.list_roles(instance)
    except Exception as exc:
        return _oracle_read_degraded(instance, "角色列表", exc, {"roles": []})


@router.get("/{instance_id}/users/{username}/privileges")
def oracle_user_privileges(instance_id: int, username: str, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return user_mgmt_service.list_user_privs(instance, username)


@router.post("/{instance_id}/users/create-script")
def oracle_create_user_script(
    instance_id: int,
    username: str = Query(...), password: str = Query(...),
    default_tablespace: str = Query("USERS"), temp_tablespace: str = Query("TEMP"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_create_user_script(username, password, default_tablespace, temp_tablespace)


@router.post("/{instance_id}/users/{username}/drop-script")
def oracle_drop_user_script(instance_id: int, username: str, cascade: bool = Query(False), db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_drop_user_script(username, cascade)


@router.post("/{instance_id}/users/{username}/lock-script")
def oracle_lock_user_script(instance_id: int, username: str, action: str = Query("lock", pattern="^(lock|unlock)$"), db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_lock_user_script(username, action)


@router.post("/{instance_id}/users/{username}/password-script")
def oracle_password_script(instance_id: int, username: str, new_password: str = Query(...), db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_password_script(username, new_password)


@router.post("/{instance_id}/grant-script")
def oracle_grant_script(
    instance_id: int, username: str = Query(...), privileges: str = Query(...),
    on_object: str | None = Query(None), db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_grant_script(username, privileges, on_object)


@router.post("/{instance_id}/revoke-script")
def oracle_revoke_script(
    instance_id: int, username: str = Query(...), privileges: str = Query(...),
    on_object: str | None = Query(None), db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    return user_mgmt_service.generate_revoke_script(username, privileges, on_object)


@router.post("/{instance_id}/ssh-test")
def oracle_ssh_test(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return ssh_service.test_ssh_connection(instance)


@router.post("/{instance_id}/ssh-diagnose")
def oracle_ssh_diagnose(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return ssh_service.diagnose_ssh_connection(instance)


@router.get("/{instance_id}/ssh-system-info")
def oracle_ssh_system_info(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return ssh_service.get_system_info(instance)


@router.post("/{instance_id}/ssh-execute")
def oracle_ssh_execute(
    instance_id: int,
    command: str = Query(...),
    confirm: bool = Query(False),
    db: Session = Depends(get_db),
):
    if not confirm:
        raise HTTPException(status_code=400, detail="执行远程命令需要 confirm=true")
    instance = _get_oracle_instance(db, instance_id)
    return ssh_service.execute_command(instance, command)


@router.post("/{instance_id}/tablespaces/deploy-autoextend")
def oracle_deploy_autoextend(
    instance_id: int,
    threshold_pct: float = Query(85.0),
    remote_path: str = Query("/home/oracle/scripts/autoextend.sh"),
    run: bool = Query(False),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    script = _build_autoextend_script(instance, threshold_pct)
    return ssh_service.deploy_script(instance, script, remote_path, run)


@router.post("/{instance_id}/archive/deploy-cleanup")
def oracle_deploy_cleanup(
    instance_id: int,
    retention_days: int = Query(7),
    remote_path: str = Query("/home/oracle/scripts/archive_cleanup.sh"),
    run: bool = Query(False),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    script = _build_archive_cleanup_script(instance, retention_days)
    return ssh_service.deploy_script(instance, script, remote_path, run)


@router.get("/{instance_id}/archive-space-detail")
def oracle_archive_space_detail(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return archive_fra_service.get_archive_space_detail(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "归档空间详情",
            exc,
            {
                "fra": [],
                "fra_used_pct": 0,
                "fra_status": "unknown",
                "usage_by_type": [],
                "log_summary": [],
                "recent_logs": [],
                "redo_groups": [],
                "current_sequence": 0,
            },
        )


@router.get("/{instance_id}/archive-emergency-script")
def oracle_archive_emergency_script(
    instance_id: int,
    mode: str = Query("rman", pattern="^(rman|force|extend_fra)$"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return archive_fra_service.generate_emergency_cleanup_script(instance, mode)


@router.post("/{instance_id}/inspect", response_model=InspectionResponse)
def inspect_oracle(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    task_id, rows = inspection_dispatch.run_collecting_rows(
        db, instance, trigger_type="oracle_page",
    )
    return _response(task_id, instance_id, "oracle", rows)


@router.get("/{instance_id}/tablespaces")
def oracle_tablespaces(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "表空间状态")


@router.get("/{instance_id}/asm")
def oracle_asm(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "ASM 状态")


@router.get("/{instance_id}/fra")
def oracle_fra(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "FRA / 归档状态")


@router.get("/{instance_id}/top-sql")
def oracle_top_sql(instance_id: int, db: Session = Depends(get_db)):
    items, total = slow_sql_service.get_list(db, instance_id=instance_id, db_type="oracle", page_size=50)
    return {"items": [SlowSQLResponse.model_validate(item) for item in items], "total": total}


@router.get("/{instance_id}/locks")
def oracle_locks(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "会话与锁等待")


@router.get("/{instance_id}/dg-status")
def oracle_dg_status(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "Data Guard")


@router.get("/{instance_id}/dataguard/overview")
def oracle_dataguard_overview(instance_id: int, standby_instance_id: int = None, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    standby_instance = None
    if standby_instance_id:
        standby_instance = _get_oracle_instance(db, standby_instance_id)
    return oracle_dataguard.get_dataguard_overview(instance, db, standby_instance)


@router.post("/{instance_id}/dataguard/switchover-check-plan")
def oracle_dataguard_switchover_check_plan(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_dataguard.build_switchover_check_plan(instance)


@router.get("/{instance_id}/temp/overview")
def oracle_temp_overview(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_temp.get_temp_overview(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "TEMP 概览",
            exc,
            {"database_role": "", "open_mode": "", "items": [], "risks": []},
        )


@router.get("/{instance_id}/temp/files")
def oracle_temp_files(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_temp.get_temp_files(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "TEMP 文件",
            exc,
            {"items": [], "risks": []},
        )


@router.get("/{instance_id}/temp/sql-usage")
def oracle_temp_sql_usage(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_temp.get_temp_sql_usage(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "TEMP SQL 占用",
            exc,
            {"items": [], "risks": []},
        )


@router.get("/{instance_id}/temp/session-usage")
def oracle_temp_session_usage(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_temp.get_temp_session_usage(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "TEMP 会话占用",
            exc,
            {"items": []},
        )


@router.post("/{instance_id}/temp/extend-plan")
def oracle_temp_extend_plan(
    instance_id: int,
    tablespace_name: str = Query("TEMP", min_length=1, max_length=128),
    size_mb: int = Query(20480, ge=100, le=102400),
    autoextend: bool = True,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_temp.get_temp_extend_plan(instance, tablespace_name, size_mb, autoextend)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{instance_id}/rman-status")
def oracle_rman_status(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return backup_service.list_rman_backup_history(instance, days=30, limit=50)


@router.get("/{instance_id}/table-fragmentation")
def oracle_table_fragmentation(
    instance_id: int,
    owner: str | None = Query(None),
    min_size_mb: float = Query(10, ge=0),
    limit: int = Query(100, ge=10, le=500),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return table_index_service.get_table_fragmentation(instance, owner, min_size_mb, limit)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "表碎片分析",
            exc,
            {
                "schemas": [],
                "tables": [],
                "summary": {
                    "total_tables": 0,
                    "total_allocated_mb": 0,
                    "total_used_mb": 0,
                    "total_reserved_mb": 0,
                    "total_waste_mb": 0,
                    "waste_ratio_pct": 0,
                },
            },
        )


@router.get("/{instance_id}/table-shrink-script")
def oracle_table_shrink_script(
    instance_id: int,
    owner: str = Query(...),
    table_name: str = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.generate_shrink_script(instance, owner, table_name)


@router.get("/{instance_id}/index-fragmentation")
def oracle_index_fragmentation(
    instance_id: int,
    owner: str = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.get_index_fragmentation(instance, owner)


@router.get("/{instance_id}/index-rebuild-script")
def oracle_index_rebuild_script(
    instance_id: int,
    owner: str = Query(...),
    index_name: str = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.generate_rebuild_index_script(instance, owner, index_name)


@router.get("/{instance_id}/unused-indexes")
def oracle_unused_indexes(
    instance_id: int,
    owner: str = Query(...),
    days: int = Query(90, ge=7, le=365),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.get_unused_indexes(instance, owner, days)


@router.get("/{instance_id}/drop-index-script")
def oracle_drop_index_script(
    instance_id: int,
    owner: str = Query(...),
    index_name: str = Query(...),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.generate_drop_index_script(instance, owner, index_name)


@router.get("/{instance_id}/similar-tables")
def oracle_similar_tables(
    instance_id: int,
    owner: str = Query(...),
    pattern: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.find_similar_tables(instance, owner, pattern, limit)


from pydantic import BaseModel as _BaseModel


class BatchAlterColumnRequest(_BaseModel):
    owner: str
    table_names: list[str]
    action: str
    column_name: str
    data_type: str
    data_length: int | None = None
    data_precision: int | None = None
    data_scale: int | None = None
    nullable: bool = True
    default_value: str | None = None


@router.post("/{instance_id}/batch-alter-column")
def oracle_batch_alter_column(
    instance_id: int,
    data: BatchAlterColumnRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return table_index_service.generate_batch_alter_sql(
        instance,
        data.owner,
        data.table_names,
        data.action,
        data.column_name,
        data.data_type,
        data.data_length,
        data.data_precision,
        data.data_scale,
        data.nullable,
        data.default_value,
    )


class BatchCreateIndexRequest(_BaseModel):
    owner: str
    table_names: list[str]
    index_suffix: str = "BATCH"
    columns: str
    tablespace: str | None = None
    unique: bool = False
    parallel: int | None = None
    online: bool = False


@router.post("/{instance_id}/batch-create-index")
def oracle_batch_create_index(
    instance_id: int,
    data: BatchCreateIndexRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    return table_index_service.generate_batch_create_index_sql(
        data.owner,
        data.table_names,
        data.index_suffix,
        data.columns,
        data.tablespace,
        data.unique,
        data.parallel,
        data.online,
    )


@router.get("/{instance_id}/slow-sql-realtime")
def oracle_slow_sql_realtime(
    instance_id: int,
    category: str = Query("top_avg", pattern="^(active|top_elapsed|top_avg|top_buffer|top_disk|waiting)$"),
    hours: int = Query(1, ge=1, le=168, description="Last-active cursor filter in hours; statistics remain cumulative, not interval deltas"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    return slow_sql_analysis_service.get_slow_sql_list(instance, category, hours=hours)


@router.get("/{instance_id}/sql-detail")
def oracle_sql_detail(
    instance_id: int,
    sql_id: str = Query(...),
    db: Session = Depends(get_db),
    child_number: int | None = Query(None, ge=0),
    con_id: int | None = Query(None, ge=0),
    plan_hash_value: int | None = Query(None, ge=0),
):
    instance = _get_oracle_instance(db, instance_id)
    result = slow_sql_analysis_service.get_sql_detail(instance, sql_id, child_number=child_number,
                                                    con_id=con_id, plan_hash_value=plan_hash_value)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    if result.get("status") == "degraded":
        raise HTTPException(status_code=502, detail=result["message"])
    return result


@router.post("/{instance_id}/archive-file/csv-plan")
def oracle_archive_csv_plan(
    instance_id: int,
    payload: CsvPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_csv_sqlcl_plan(payload.model_dump()) if payload.csv_method.upper() == "SQLCL" else \
               oracle_archive_file.build_csv_sqlplus_plan(payload.model_dump()) if payload.csv_method.upper() == "SQLPLUS" else \
               oracle_archive_file.build_csv_python_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/archive-file/expdp-plan")
def oracle_archive_expdp_plan(
    instance_id: int,
    payload: ExpdpPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_expdp_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/archive-file/impdp-plan")
def oracle_archive_impdp_plan(
    instance_id: int,
    payload: ExpdpPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_impdp_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/archive-file/directory-plan")
def oracle_archive_directory_plan(
    instance_id: int,
    payload: DirectoryPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_directory_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/archive-file/verify-plan")
def oracle_archive_verify_plan(
    instance_id: int,
    payload: VerifyPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_archive_file_verify_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/archive-file/full-plan")
def oracle_archive_full_plan(
    instance_id: int,
    payload: FullArchiveFilePlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_archive_file.build_full_archive_file_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/tables")
def oracle_stats_tables(
    instance_id: int,
    owner: str = None,
    table_name: str = None,
    stale_only: bool = False,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.list_table_stats(instance, owner, table_name, stale_only)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/table-detail")
def oracle_stats_table_detail(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.get_table_stats_detail(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/columns")
def oracle_stats_columns(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.list_column_stats(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/indexes")
def oracle_stats_indexes(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.list_index_stats(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/history")
def oracle_stats_history(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.list_stats_history(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/stats/operations")
def oracle_stats_operations(
    instance_id: int,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.list_stats_operations(instance)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/stats/gather-table-plan")
def oracle_stats_gather_table_plan(
    instance_id: int,
    payload: GatherTableStatsRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_stats.build_gather_table_stats_plan(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/stats/gather-schema-plan")
def oracle_stats_gather_schema_plan(
    instance_id: int,
    payload: GatherSchemaStatsRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_stats.build_gather_schema_stats_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/stats/lock-table-plan")
def oracle_stats_lock_table_plan(
    instance_id: int,
    payload: TableStatsPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_stats.build_lock_table_stats_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/stats/unlock-table-plan")
def oracle_stats_unlock_table_plan(
    instance_id: int,
    payload: TableStatsPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_stats.build_unlock_table_stats_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/stats/delete-table-plan")
def oracle_stats_delete_table_plan(
    instance_id: int,
    payload: TableStatsPlanRequest,
    db: Session = Depends(get_db),
):
    try:
        return oracle_stats.build_delete_table_stats_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/partition/candidates")
def oracle_partition_candidates(
    instance_id: int,
    owner: str | None = None,
    table_name: str | None = None,
    min_rows: int = Query(5_000_000, ge=1),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.list_partition_candidates(instance, owner, table_name, min_rows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/partition/table-profile")
def oracle_partition_table_profile(
    instance_id: int,
    owner: str,
    table_name: str,
    exact_count: bool = False,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.get_table_profile(instance, owner, table_name, exact_count)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/partition/key-candidates")
def oracle_partition_key_candidates(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.suggest_partition_keys(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/partition/convert-plan")
def oracle_partition_convert_plan(instance_id: int, payload: PartitionPlanRequest, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.build_partition_convert_plan(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/partition/redefinition-plan")
def oracle_partition_redefinition_plan(instance_id: int, payload: PartitionPlanRequest, db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.build_redefinition_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/partition/ctas-plan")
def oracle_partition_ctas_plan(instance_id: int, payload: PartitionPlanRequest, db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_partition.build_ctas_partition_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/indexes/unusable")
def oracle_unusable_indexes(
    instance_id: int,
    owner: str = None,
    table_name: str = None,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.list_unusable_indexes(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/indexes/partitions/unusable")
def oracle_unusable_index_partitions(
    instance_id: int,
    owner: str = None,
    table_name: str = None,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.list_unusable_index_partitions(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/indexes/abnormal")
def oracle_abnormal_indexes(
    instance_id: int,
    owner: str = None,
    table_name: str = None,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.list_abnormal_indexes(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/indexes/rebuild-plan")
def oracle_rebuild_index_plan(
    instance_id: int,
    payload: RebuildIndexPlanRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.build_rebuild_index_plan(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/indexes/create-plan")
def oracle_create_index_plan(
    instance_id: int,
    payload: CreateIndexPlanRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.build_create_index_plan(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/indexes/check-duplicate")
def oracle_check_duplicate_index(
    instance_id: int,
    payload: CheckDuplicateRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.check_duplicate_index(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/tables/{owner}/{table_name}/columns")
def oracle_table_columns(
    instance_id: int,
    owner: str,
    table_name: str,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_index.list_table_columns(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/db-link-plan")
def archive_db_dblink_plan(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_db_link_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/create-history-table-plan")
def archive_db_create_history_table(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_create_history_table_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/insert-select-plan")
def archive_db_insert_select(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_insert_select_cross_db_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/datapump-network-plan")
def archive_db_datapump_network(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_datapump_network_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/verify-plan")
def archive_db_verify(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_verify_cross_db_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/delete-source-plan")
def archive_db_delete_source(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_delete_source_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/gather-stats-plan")
def archive_db_gather_stats(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_gather_stats_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/archive-db/full-plan")
def archive_db_full_plan(payload: ArchiveDbPlanRequest):
    try:
        return oracle_archive_db.build_full_archive_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _get_oracle_instance(db: Session, instance_id: int):
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.db_type != "oracle":
        raise HTTPException(status_code=400, detail="Instance is not Oracle")
    return instance


def _latest_category(db: Session, instance_id: int, category: str):
    items, total = inspection_service.list_results(db, instance_id=instance_id, db_type="oracle", page_size=100)
    filtered = [item for item in items if item.check_category == category]
    return {"items": [CheckResultResponse.model_validate(item) for item in filtered], "total": len(filtered)}


def _response(task_id: int, instance_id: int, db_type: str, rows: list) -> InspectionResponse:
    warning = len([row for row in rows if row.check_status == "warning"])
    critical = len([row for row in rows if row.check_status == "critical"])
    return InspectionResponse(task_id=task_id, instance_id=instance_id, db_type=db_type, total=len(rows), warning=warning, critical=critical, results=rows)


def _get_lock_details(instance: DBInstance) -> dict:
    from app.collectors.oracle.oracle_connection import oracle_connection
    from app.collectors.utils import rows_to_dicts, safe_rows
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT s.sid, s.serial# AS serial_num, s.username, s.status, s.machine, s.program, "
                "s.sql_id, s.last_call_et, s.blocking_session, s.wait_class, s.seconds_in_wait, p.spid "
                "FROM v$session s LEFT JOIN v$process p ON p.addr = s.paddr "
                "WHERE s.username IS NOT NULL ORDER BY s.sid"
            )
            sessions = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            sessions = []
        try:
            cursor.execute(
                "SELECT l.session_id, l.oracle_username, l.os_user_name, l.locked_mode, "
                "o.object_name, o.object_type "
                "FROM v$locked_object l JOIN dba_objects o ON l.object_id = o.object_id"
            )
            locks = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            locks = []
    return {"sessions": sessions, "locked_objects": locks}


def _get_blocking_tree(instance: DBInstance, *, strict: bool = False) -> dict:
    from app.collectors.oracle.oracle_connection import oracle_connection
    from app.collectors.utils import rows_to_dicts, safe_rows

    BLOCKING_TREE_SQL = """
WITH
cs AS (
  SELECT
    s.sid,
    s.serial# AS serial_num,
    s.username,
    s.status,
    s.sql_id,
    s.module,
    s.machine,
    s.program,
    s.event,
    s.seconds_in_wait,
    NULLIF(s.blocking_session, 0) AS blocking_sid,
    p.spid
  FROM v$session s
  JOIN v$process p ON p.addr = s.paddr
  WHERE s.username IS NOT NULL
),
edges AS (
  SELECT w.sid AS waiter_sid, w.serial_num AS waiter_serial, w.blocking_sid AS blocker_sid
  FROM cs w WHERE w.blocking_sid IS NOT NULL
),
roots AS (
  SELECT DISTINCT b.blocker_sid AS root_sid
  FROM edges b
  JOIN cs s ON s.sid = b.blocker_sid
  WHERE s.blocking_sid IS NULL
),
chain_only AS (
  SELECT * FROM cs
  WHERE sid IN (SELECT root_sid FROM roots)
     OR sid IN (SELECT waiter_sid FROM edges)
     OR sid IN (SELECT blocker_sid FROM edges)
),
blocker_mark AS (
  SELECT blocker_sid AS sid, COUNT(*) AS waiters FROM edges GROUP BY blocker_sid
)
SELECT
  LPAD(' ', 2*(LEVEL-1)) ||
  CASE WHEN LEVEL = 1 THEN '[ROOT] ' ELSE '  |- ' END ||
  chain_only.sid || ',' || chain_only.serial_num ||
  ' spid=' || chain_only.spid ||
  ' ' || NVL(chain_only.username, '-') ||
  CASE WHEN bm.waiters IS NOT NULL THEN ' (BLOCKER)' ELSE ' (WAITER)' END
  AS tree,
  LEVEL AS tree_level,
  chain_only.sid,
  chain_only.serial_num,
  chain_only.spid,
  chain_only.username,
  chain_only.status,
  chain_only.blocking_sid,
  chain_only.seconds_in_wait AS wait_s,
  SUBSTR(chain_only.event, 1, 40) AS event,
  chain_only.sql_id,
  SUBSTR(chain_only.module, 1, 22) AS module,
  SUBSTR(chain_only.machine, 1, 22) AS machine,
  SUBSTR(chain_only.program, 1, 28) AS program,
  CASE WHEN bm.waiters IS NOT NULL THEN 1 ELSE 0 END AS is_blocker,
  NVL(bm.waiters, 0) AS waiter_count,
  'alter system kill session ''' || chain_only.sid || ',' || chain_only.serial_num || ''' immediate;' AS kill_sql
FROM chain_only
LEFT JOIN blocker_mark bm ON bm.sid = chain_only.sid
CONNECT BY NOCYCLE
  PRIOR chain_only.sid = chain_only.blocking_sid
START WITH (chain_only.sid) IN (SELECT root_sid FROM roots)
ORDER SIBLINGS BY NVL(chain_only.seconds_in_wait, 0) DESC, chain_only.sid
"""

    BLOCKING_SIMPLE_SQL = """
SELECT
  s.sid, s.serial# AS serial_num, s.username, s.status, s.machine, s.program,
  s.sql_id, s.event, s.seconds_in_wait, s.blocking_session,
  s.module, s.row_wait_obj# AS row_wait_obj_id,
  q.sql_text
FROM v$session s LEFT JOIN v$sql q ON s.sql_id = q.sql_id
WHERE s.username IS NOT NULL
  AND (s.blocking_session IS NOT NULL
    OR s.sid IN (SELECT blocking_session FROM v$session WHERE blocking_session IS NOT NULL))
ORDER BY s.sid
"""

    LOCK_CONFLICT_SQL = """
SELECT l1.sid AS blocker_sid, l1.type AS lock_type, l1.id1, l1.id2,
  DECODE(l1.lmode, 1,'NULL',2,'RS',3,'RX',4,'S',5,'SRX',6,'X') AS blocker_mode_name,
  l2.sid AS waiter_sid,
  DECODE(l2.request, 1,'NULL',2,'RS',3,'RX',4,'S',5,'SRX',6,'X') AS waiter_request_name
FROM v$lock l1 JOIN v$lock l2 ON l1.id1 = l2.id1 AND l1.id2 = l2.id2 AND l1.type = l2.type
WHERE l1.sid != l2.sid AND l1.lmode > 0 AND l2.request > 0
  AND (l1.lmode = 6 OR l2.request = 6 OR (l1.lmode >= 5 AND l2.request >= 1))
ORDER BY l1.sid
"""

    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        tree_rows = []
        tree_query_succeeded = False
        tree_query_errors = []
        try:
            cursor.execute(BLOCKING_TREE_SQL)
            tree_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            tree_query_succeeded = True
        except Exception as exc:
            tree_query_errors.append(exc)

        if not tree_rows:
            try:
                cursor.execute(BLOCKING_SIMPLE_SQL)
                tree_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                tree_query_succeeded = True
                for r in tree_rows:
                    r["tree_level"] = 1 if not r.get("blocking_session") else 2
                    r["is_blocker"] = 1 if not r.get("blocking_session") else 0
                    r["waiter_count"] = 0
                    r["kill_sql"] = f"alter system kill session '{r['sid']},{r.get('serial_num', r.get('serial#', ''))}' immediate;"
            except Exception as exc:
                tree_query_errors.append(exc)

        if strict and not tree_query_succeeded:
            raise RuntimeError("oracle_lock_query_failed") from tree_query_errors[-1]

        lock_pairs = []
        try:
            cursor.execute(LOCK_CONFLICT_SQL)
            lock_pairs = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            pass

        obj_ids = [str(r.get("row_wait_obj_id", 0)) for r in tree_rows if r.get("row_wait_obj_id") and r.get("row_wait_obj_id", 0) > 0]
        obj_map = {}
        if obj_ids:
            try:
                cursor.execute(f"SELECT object_id, owner || '.' || object_name AS object_name FROM dba_objects WHERE object_id IN ({','.join(obj_ids)})")
                obj_map = {str(r["object_id"]): r["object_name"] for r in safe_rows(rows_to_dicts(cursor, cursor.fetchall()))}
            except Exception:
                pass

    for r in tree_rows:
        obj_id = r.get("row_wait_obj_id", -1)
        r["wait_object"] = obj_map.get(str(obj_id), "") if obj_id and obj_id > 0 else ""

    blocker_count = len([r for r in tree_rows if r.get("is_blocker")])
    waiter_count = len([r for r in tree_rows if not r.get("is_blocker")])
    max_wait = max((r.get("wait_s", 0) or 0 for r in tree_rows), default=0)

    return {
        "tree": tree_rows,
        "lock_pairs": lock_pairs,
        "summary": {
            "blocker_count": blocker_count,
            "waiter_count": waiter_count,
            "total": len(tree_rows),
            "max_wait_seconds": max_wait,
        },
    }


def _capacity_summary(instance: DBInstance) -> dict:
    from app.collectors.oracle.oracle_connection import oracle_connection
    from app.collectors.utils import rows_to_dicts, safe_rows
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tablespace_name, ROUND(total_mb,2) AS total_mb, ROUND(used_mb,2) AS used_mb, "
            "ROUND(free_mb,2) AS free_mb, ROUND(used_pct,2) AS used_pct "
            "FROM (SELECT t.tablespace_name, t.total_mb, nvl(s.used_mb,0) AS used_mb, "
            "t.total_mb - nvl(s.used_mb,0) AS free_mb, "
            "ROUND(nvl(s.used_mb,0)/t.total_mb*100,2) AS used_pct "
            "FROM (SELECT tablespace_name, SUM(bytes)/1024/1024 AS total_mb FROM dba_data_files GROUP BY tablespace_name) t "
            "LEFT JOIN (SELECT tablespace_name, SUM(bytes)/1024/1024 AS used_mb FROM dba_segments GROUP BY tablespace_name) s "
            "ON t.tablespace_name = s.tablespace_name) ORDER BY used_pct DESC"
        )
        tablespaces = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        total_db = sum(t.get("total_mb", 0) for t in tablespaces)
        used_db = sum(t.get("used_mb", 0) for t in tablespaces)
    return {"tablespaces": tablespaces, "total_mb": round(total_db, 2), "used_mb": round(used_db, 2), "used_pct": round(used_db / total_db * 100, 2) if total_db > 0 else 0}


def _capacity_growth(instance: DBInstance, days: int) -> dict:
    from app.collectors.oracle.oracle_connection import oracle_connection
    from app.collectors.utils import rows_to_dicts, safe_rows
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT tablespace_name, ROUND(SUM(bytes)/1024/1024,2) AS total_mb "
                f"FROM dba_data_files GROUP BY tablespace_name ORDER BY tablespace_name"
            )
            current = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            current = []
    return {"days": days, "current_snapshot": current, "message": "容量增长趋势需配合 db_object_size_daily 日快照数据，V1 返回当前快照。"}


def _build_autoextend_script(instance: DBInstance, threshold_pct: float) -> str:
    return (
        "#!/bin/bash\n"
        f"# Oracle 表空间自动扩容脚本\n"
        f"# 实例: {instance.instance_name}\n"
        f"# 阈值: {threshold_pct}%\n"
        f"# 请通过 crontab -e 添加定时任务，例如每30分钟检查一次:\n"
        f"# */30 * * * * /home/oracle/scripts/autoextend.sh >> /home/oracle/scripts/autoextend.log 2>&1\n\n"
        f"export ORACLE_SID={instance.service_name or instance.instance_name}\n"
        f"THRESHOLD={threshold_pct}\n\n"
        f'sqlplus -S / as sysdba <<EOF\n'
        f"SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF\n"
        f"SELECT tablespace_name, ROUND(used_pct,1), total_mb\n"
        f"FROM (\n"
        f"  SELECT t.tablespace_name, t.total_mb,\n"
        f"    ROUND(nvl(s.used_mb,0)/t.total_mb*100,2) AS used_pct\n"
        f"  FROM (SELECT tablespace_name, SUM(bytes)/1024/1024 AS total_mb FROM dba_data_files GROUP BY tablespace_name) t\n"
        f"  LEFT JOIN (SELECT tablespace_name, SUM(bytes)/1024/1024 AS used_mb FROM dba_segments GROUP BY tablespace_name) s\n"
        f"  ON t.tablespace_name = s.tablespace_name\n"
        f") WHERE used_pct > {threshold_pct};\n"
        f"EXIT;\n"
        f"EOF\n"
    )


def _build_archive_cleanup_script(instance: DBInstance, retention_days: int) -> str:
    return (
        "#!/bin/bash\n"
        f"# Oracle 归档日志清理脚本\n"
        f"# 实例: {instance.instance_name}\n"
        f"# 保留天数: {retention_days}\n"
        f"# 请通过 crontab -e 添加定时任务，例如每天凌晨2点执行:\n"
        f"# 0 2 * * * /home/oracle/scripts/archive_cleanup.sh >> /home/oracle/scripts/archive_cleanup.log 2>&1\n\n"
        f"export ORACLE_SID={instance.service_name or instance.instance_name}\n"
        f"RETENTION_DAYS={retention_days}\n\n"
        f"rman target / <<EOF\n"
        f"CROSSCHECK ARCHIVELOG ALL;\n"
        f"DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;\n"
        f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-{retention_days}';\n"
        f"EXIT;\n"
        f"EOF\n"
    )


@router.get("/{instance_id}/table-usage/hot-tables")
def oracle_table_usage_hot_tables(
    instance_id: int,
    days: int = Query(30, ge=1, le=365),
    source: str = Query("AWR"),
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_hot_tables(instance, days, source, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/table-sqls")
def oracle_table_usage_table_sqls(
    instance_id: int,
    owner: str = Query(...),
    table_name: str = Query(...),
    days: int = Query(30, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_table_sqls(instance, owner, table_name, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/full-scan-sqls")
def oracle_table_usage_full_scan_sqls(
    instance_id: int,
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_full_scan_sqls(instance, owner, table_name, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/index-usage")
def oracle_table_usage_index_usage(
    instance_id: int,
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    days: int = Query(90, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_index_usage(instance, owner, table_name, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/unused-indexes")
def oracle_table_usage_unused_indexes(
    instance_id: int,
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    days: int = Query(90, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_unused_indexes(instance, owner, table_name, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/duplicate-indexes")
def oracle_table_usage_duplicate_indexes(
    instance_id: int,
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_duplicate_indexes(instance, owner, table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{instance_id}/table-usage/index-advice")
def oracle_table_usage_index_advice(
    instance_id: int,
    owner: str | None = Query(None),
    table_name: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.list_index_advice(instance, owner, table_name, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/table-usage/collect")
def oracle_table_usage_collect(
    instance_id: int,
    days: int = Query(30, ge=1, le=365),
    source: str = Query("AWR"),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.collect_table_usage(instance, days, source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/table-usage/generate-index-advice")
def oracle_table_usage_generate_index_advice(
    instance_id: int,
    payload: TableUsageIndexAdviceRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.generate_index_advice(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/table-usage/index-invisible-plan")
def oracle_table_usage_index_invisible_plan(
    instance_id: int,
    payload: IndexObjectPlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.build_index_invisible_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/table-usage/drop-index-plan")
def oracle_table_usage_drop_index_plan(
    instance_id: int,
    payload: IndexObjectPlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.build_drop_index_plan(payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/table-usage/create-index-plan")
def oracle_table_usage_create_index_plan(
    instance_id: int,
    payload: TableUsageCreateIndexPlanRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_table_usage.build_create_index_plan(instance, payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/{instance_id}/undo/overview')
def oracle_undo_overview(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return undo_service.get_undo_overview(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "UNDO 概览",
            exc,
            {
                "undo_tablespace": "",
                "undo_retention": 0,
                "tuned_undoretention": 0,
                "total_mb": 0,
                "used_mb": 0,
                "used_pct": 0,
                "risk_level": "unknown",
                "risks": [],
            },
        )


@router.get('/{instance_id}/undo/extents')
def oracle_undo_extents(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return undo_service.get_undo_extents(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "UNDO 空间明细",
            exc,
            {"items": [], "summary": []},
        )


@router.get('/{instance_id}/undo/transactions')
def oracle_undo_transactions(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return undo_service.get_undo_transactions(instance)
    except Exception as exc:
        return _oracle_read_degraded(
            instance,
            "UNDO 活跃事务",
            exc,
            {"items": []},
        )


@router.get('/{instance_id}/undo/history')
def oracle_undo_history(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return undo_service.get_undo_history(instance)
    except Exception as exc:
        logger.warning("Oracle read degraded | instance_id=%s | operation=UNDO history | error=%s", instance.id, exc)
        return []


@router.post('/{instance_id}/undo/retention-plan')
def oracle_undo_retention_plan(instance_id: int, payload: UndoRetentionPlanRequest, db: Session = Depends(get_db)):
    _get_oracle_instance(db, instance_id)
    try:
        return undo_service.build_undo_retention_plan(
            retention_seconds=payload.retention_seconds,
            scope=payload.scope,
            rac_all_instances=payload.rac_all_instances,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
