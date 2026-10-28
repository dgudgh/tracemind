from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import generic_db_ops
from app.services import instances as instance_service

router = APIRouter(prefix="/operations", tags=["PostgreSQL / SQL Server 运维"])


def _get(db: Session, engine: str, instance_id: int):
    if engine not in ("postgresql", "sqlserver"):
        raise HTTPException(status_code=404, detail="Unsupported engine")
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    if obj.db_type != engine:
        raise HTTPException(status_code=400, detail=f"Instance is not {engine}")
    return obj


@router.get("/{engine}/modules")
def api_modules(engine: str):
    if engine not in ("postgresql", "sqlserver"):
        raise HTTPException(status_code=404, detail="Unsupported engine")
    return generic_db_ops.get_modules(engine)


@router.get("/{engine}/{instance_id}/overview")
def api_overview(engine: str, instance_id: int, db: Session = Depends(get_db)):
    return generic_db_ops.collect_section(_get(db, engine, instance_id), "overview")


@router.get("/{engine}/{instance_id}/{section}")
def api_section(engine: str, instance_id: int, section: str, db: Session = Depends(get_db)):
    return generic_db_ops.collect_section(_get(db, engine, instance_id), section)


@router.post("/{engine}/{instance_id}/plans/{plan_type}")
def api_plan(engine: str, instance_id: int, plan_type: str, payload: dict | None = None, db: Session = Depends(get_db)):
    return generic_db_ops.build_plan(_get(db, engine, instance_id), plan_type, payload)
