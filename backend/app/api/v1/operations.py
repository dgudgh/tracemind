from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.schemas.operations import (
    ArchiveCleanupPlanRequest,
    OracleOperationRequest,
    TablespaceExpansionPlanRequest,
)
from app.schemas.oracle_recovery import (
    FlashbackQueryRequest,
    RecyclebinRestorePlanRequest,
    RmanRestorePlanRequest,
    TableRestorePlanRequest,
)
from app.services import instances as instance_service
from app.services import oracle_operations
from app.services import oracle_recovery
from app.services import operations_capabilities
from app.services.access_control import is_action_allowed, resolve_scope

router = APIRouter(prefix="/operations", tags=["日常运维"])


def _capability_scope(request: Request, db: Session) -> tuple[list, bool]:
    auth = getattr(request.state, "auth", {}) or {}
    query = db.query(DBInstance).order_by(DBInstance.id)
    if auth.get("role") == "admin" or auth.get("source") not in {"db", "identity"}:
        return query.all(), True
    user_id = int(auth.get("user_id") or 0)
    if not user_id:
        return query.all(), True
    scope = resolve_scope(db, user_id)
    if scope.restricted:
        query = query.filter(DBInstance.id.in_(scope.native_instance_ids or {-1}))
    can_execute, _ = is_action_allowed(db, user_id, "operations.execute")
    return query.all(), can_execute


@router.get("/capability-matrix")
def operation_capability_matrix(
    request: Request,
    engine: str | None = Query(None),
    instance_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    instances, can_execute = _capability_scope(request, db)
    try:
        return operations_capabilities.build_capability_matrix(
            db,
            instances=instances,
            engine=engine,
            selected_instance_id=instance_id,
            can_execute=can_execute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/oracle/modules")
def oracle_modules():
    return {
        "items": [
            {"key": "one_click", "title": "一键运行", "description": "Trace、健康检查、PFILE/控制文件备份脚本、表空间与归档查看"},
            {"key": "tablespace", "title": "增强版表空间监控", "description": "表空间使用率、风险等级、扩容 SQL 方案"},
            {"key": "archive", "title": "归档日志管理", "description": "FRA 使用率、归档增长、RMAN 清理脚本方案"},
            {"key": "scripts", "title": "一键脚本执行", "description": "V1 保留入口，等待服务器 SSH 资产接入后启用"},
        ]
    }


@router.get("/oracle/instances")
def oracle_instances(db: Session = Depends(get_db)):
    items = oracle_operations.list_oracle_instances(db)
    return {
        "items": [
            {
                "id": item.id,
                "instance_name": item.instance_name,
                "host": item.host,
                "port": item.port,
                "env_type": item.env_type,
                "factory_name": item.factory_name,
            }
            for item in items
        ]
    }


@router.get("/oracle/{instance_id}/overview")
def oracle_overview(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_operations.collect_overview(instance)


@router.post("/oracle/{instance_id}/actions")
def oracle_action(instance_id: int, body: OracleOperationRequest, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_operations.run_action(instance, body.action, body.sql_text)


@router.post("/oracle/{instance_id}/inspect-ai")
def oracle_inspect_ai(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    from app.services.operation_ai import inspect_and_ai_analyze
    return inspect_and_ai_analyze(db, instance)


@router.get("/oracle/{instance_id}/tablespaces")
def oracle_tablespaces(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_operations.collect_tablespaces(instance)


@router.post("/oracle/{instance_id}/tablespaces/expansion-plan")
def oracle_tablespace_expansion_plan(
    instance_id: int,
    body: TablespaceExpansionPlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    return oracle_operations.build_tablespace_expansion_plan(
        body.tablespace_name,
        body.datafile_path,
        body.initial_size_gb,
        body.max_size_gb,
        body.autoextend,
    )


@router.get("/oracle/{instance_id}/archive")
def oracle_archive(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_operations.collect_archive(instance)


@router.post("/oracle/{instance_id}/archive/cleanup-plan")
def oracle_archive_cleanup_plan(
    instance_id: int,
    body: ArchiveCleanupPlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    return oracle_operations.build_archive_cleanup_plan(
        body.retention_days, body.cleanup_mode, body.force_cleanup
    )


@router.get("/oracle/{instance_id}/alert-log")
def oracle_alert_log(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_oracle_instance(db, instance_id)
    return oracle_operations.collect_alert_log(instance)


def _get_oracle_instance(db: Session, instance_id: int):
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.db_type != "oracle":
        raise HTTPException(status_code=400, detail="Instance is not Oracle")
    return instance


@router.post("/oracle/{instance_id}/flashback/query")
def oracle_flashback_query(
    instance_id: int,
    payload: FlashbackQueryRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_recovery.flashback_query(instance, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        message = str(e)
        if "ORA-01466" in message:
            message = (
                "闪回查询失败：所选时间点前后表定义发生过变更，Oracle 无法按当前表结构读取该时间点的数据。"
                "请尝试选择表结构变更之后的时间点/SCN，或使用 Flashback Table、RMAN/TSPITR 等恢复方案。"
            )
        elif "ORA-00942" in message:
            message = (
                f"闪回查询失败：当前连接账号看不到 {payload.schema_name}.{payload.table_name}。"
                "请确认 Schema 和表名是否填写正确、对象是否存在，以及当前账号是否具备该表的 SELECT 权限。"
            )
        status_code = 400 if "ORA-" in str(e) else 500
        raise HTTPException(status_code=status_code, detail=message)


@router.get("/oracle/{instance_id}/recyclebin")
def oracle_recyclebin_list(
    instance_id: int,
    owner: str | None = Query(None),
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_recovery.list_recyclebin(instance, owner)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/oracle/{instance_id}/recyclebin/restore-plan")
def oracle_recyclebin_restore_plan(
    instance_id: int,
    payload: RecyclebinRestorePlanRequest,
    db: Session = Depends(get_db),
):
    instance = _get_oracle_instance(db, instance_id)
    try:
        return oracle_recovery.build_recyclebin_restore_plan(
            owner=payload.owner,
            object_name=payload.object_name,
            restore_name=payload.restore_name,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/oracle/{instance_id}/table/restore-plan")
def oracle_table_restore_plan(
    instance_id: int,
    payload: TableRestorePlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_recovery.build_table_restore_plan(
            owner=payload.owner,
            table_name=payload.table_name,
            restore_type=payload.restore_type,
            restore_time=payload.restore_time,
            scn=payload.scn,
            auxiliary_dest=payload.auxiliary_dest,
            remap_name=payload.remap_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/oracle/{instance_id}/rman/restore-plan")
def oracle_rman_restore_plan(
    instance_id: int,
    payload: RmanRestorePlanRequest,
    db: Session = Depends(get_db),
):
    _get_oracle_instance(db, instance_id)
    try:
        return oracle_recovery.build_rman_restore_plan(
            recovery_scope=payload.recovery_scope,
            target_name=payload.target_name,
            restore_type=payload.restore_type,
            restore_time=payload.restore_time,
            scn=payload.scn,
            include_open_resetlogs=payload.include_open_resetlogs,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
