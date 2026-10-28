from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.check_result import CheckResultResponse
from app.schemas.check_result import InspectionResponse
from app.schemas.slow_sql import SlowSQLResponse
from app.services import inspection as inspection_service
from app.services.inspection import dispatch as inspection_dispatch
from app.services import instances as instance_service
from app.services import slow_sql as slow_sql_service
from app.services.mysql_binlog import get_binlog_overview

router = APIRouter(prefix="/mysql", tags=["MySQL 运维中心"])


@router.post("/{instance_id}/inspect", response_model=InspectionResponse)
def inspect_mysql(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_mysql_instance(db, instance_id)
    task_id, rows = inspection_dispatch.run_collecting_rows(
        db, instance, trigger_type="mysql_page",
    )
    return _response(task_id, instance_id, "mysql", rows)


@router.get("/{instance_id}/status")
def mysql_status(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "连接数")


@router.get("/{instance_id}/replication")
def mysql_replication(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "主从复制")


@router.get("/{instance_id}/slow-sql")
def mysql_slow_sql(instance_id: int, db: Session = Depends(get_db)):
    items, total = slow_sql_service.get_list(db, instance_id=instance_id, db_type="mysql", page_size=50)
    return {"items": [SlowSQLResponse.model_validate(item) for item in items], "total": total}


@router.get("/{instance_id}/large-tables")
def mysql_large_tables(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "大表分析")


@router.get("/{instance_id}/fragmentation")
def mysql_fragmentation(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "表碎片分析")


@router.get("/{instance_id}/locks")
def mysql_locks(instance_id: int, db: Session = Depends(get_db)):
    return _latest_category(db, instance_id, "锁等待")


@router.get("/{instance_id}/binlog")
def mysql_binlog(instance_id: int, db: Session = Depends(get_db)):
    return get_binlog_overview(_get_mysql_instance(db, instance_id))


def _get_mysql_instance(db: Session, instance_id: int):
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.db_type != "mysql":
        raise HTTPException(status_code=400, detail="Instance is not MySQL")
    return instance


def _latest_category(db: Session, instance_id: int, category: str):
    items, total = inspection_service.list_results(db, instance_id=instance_id, db_type="mysql", page_size=100)
    filtered = [item for item in items if item.check_category == category]
    return {"items": [CheckResultResponse.model_validate(item) for item in filtered], "total": len(filtered)}


def _response(task_id: int, instance_id: int, db_type: str, rows: list) -> InspectionResponse:
    warning = len([row for row in rows if row.check_status == "warning"])
    critical = len([row for row in rows if row.check_status == "critical"])
    return InspectionResponse(task_id=task_id, instance_id=instance_id, db_type=db_type, total=len(rows), warning=warning, critical=critical, results=rows)
