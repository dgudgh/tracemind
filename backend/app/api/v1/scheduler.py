"""统一定时任务 API — 聚合容量/AWR/性能基线三类按实例策略，供「定时任务」页统一管理。

各类领域配置仍存于各自策略表；这里只统一调度视图与开关/间隔/立即执行。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.models.capacity_policy import DBCapacityPolicy
from app.models.awr_collect_policy import DBAwrCollectPolicy
from app.models.perf_metric_policy import DBPerfMetricPolicy
from app.services.access_control import resolve_scope

logger = logging.getLogger("app.api.scheduler")
router = APIRouter(prefix="/scheduler", tags=["定时任务"])

TASK_LABELS = {"capacity": "容量巡检", "awr": "AWR 自动采集", "perf_baseline": "性能基线采集"}


class TaskUpdate(BaseModel):
    enabled: Optional[int] = Field(None, ge=0, le=1)
    interval_minutes: Optional[int] = Field(None, ge=1, le=20160)


def _iso(dt):
    return dt.isoformat(sep=" ") if dt else None


def _allowed_instance_ids(request: Request, db: Session, minimum_access: str = "read") -> set[int] | None:
    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("role") == "admin":
        return None
    scope = resolve_scope(db, int(auth.get("user_id") or 0), minimum_access=minimum_access)
    return set(scope.native_instance_ids) if scope.restricted else None


def _require_instance_access(request: Request, db: Session, instance_id: int, minimum_access: str) -> None:
    allowed = _allowed_instance_ids(request, db, minimum_access)
    if allowed is not None and instance_id not in allowed:
        raise HTTPException(403, "定时任务实例不在当前用户的数据范围内")


@router.get("/status")
def api_status():
    from app.tasks.master_scheduler import get_scheduler_status
    return get_scheduler_status()


@router.get("/tasks")
def api_list_tasks(request: Request, instance_id: Optional[int] = None, task_type: Optional[str] = None,
                   db: Session = Depends(get_db)):
    allowed = _allowed_instance_ids(request, db)
    instance_query = db.query(DBInstance)
    if allowed is not None:
        if not allowed:
            return {"items": []}
        instance_query = instance_query.filter(DBInstance.id.in_(allowed))
    inst_map = {i.id: i for i in instance_query.all()}

    def base(p, ttype, interval):
        inst = inst_map.get(p.instance_id)
        return {
            "task_type": ttype,
            "task_label": TASK_LABELS.get(ttype, ttype),
            "instance_id": p.instance_id,
            "instance_name": inst.instance_name if inst else str(p.instance_id),
            "db_type": inst.db_type if inst else None,
            "host": f"{inst.host}:{inst.port}" if inst else None,
            "enabled": p.enabled,
            "interval_minutes": interval,
            "last_run_time": _iso(p.last_run_time),
            "next_run_time": _iso(p.next_run_time),
            "last_status": getattr(p, "last_status", None),
            "last_message": getattr(p, "last_message", None),
        }

    items = []
    if task_type in (None, "capacity"):
        for p in db.query(DBCapacityPolicy).all():
            items.append(base(p, "capacity", p.frequency_minutes))
    if task_type in (None, "awr"):
        for p in db.query(DBAwrCollectPolicy).all():
            items.append(base(p, "awr", (p.interval_hours or 24) * 60))
    if task_type in (None, "perf_baseline"):
        for p in db.query(DBPerfMetricPolicy).all():
            items.append(base(p, "perf_baseline", p.interval_minutes))

    if allowed is not None:
        items = [item for item in items if item["instance_id"] in allowed]

    if instance_id is not None:
        items = [it for it in items if it["instance_id"] == instance_id]
    items.sort(key=lambda x: (x["instance_id"], x["task_type"]))
    return {"items": items}


def _get_or_create(db, model, instance_id, **defaults):
    p = db.query(model).filter(model.instance_id == instance_id).first()
    if not p:
        p = model(instance_id=instance_id, **defaults)
        db.add(p)
    return p


@router.put("/tasks/{task_type}/{instance_id}")
def api_update_task(task_type: str, instance_id: int, payload: TaskUpdate, request: Request, db: Session = Depends(get_db)):
    _require_instance_access(request, db, instance_id, "operate")
    if not db.query(DBInstance).filter(DBInstance.id == instance_id).first():
        raise HTTPException(404, "实例不存在")
    now = datetime.now()

    if task_type == "capacity":
        p = _get_or_create(db, DBCapacityPolicy, instance_id)
        if payload.enabled is not None:
            p.enabled = payload.enabled
        if payload.interval_minutes is not None:
            p.frequency_minutes = max(1, payload.interval_minutes)
        p.next_run_time = (p.last_run_time or now) + timedelta(minutes=p.frequency_minutes or 1440)
        interval = p.frequency_minutes
    elif task_type == "awr":
        p = _get_or_create(db, DBAwrCollectPolicy, instance_id)
        if payload.enabled is not None:
            p.enabled = payload.enabled
        if payload.interval_minutes is not None:
            p.interval_hours = max(1, (payload.interval_minutes + 59) // 60)
        p.next_run_time = (p.last_run_time or now) + timedelta(hours=p.interval_hours or 24)
        interval = (p.interval_hours or 24) * 60
    elif task_type == "perf_baseline":
        p = _get_or_create(db, DBPerfMetricPolicy, instance_id, enabled=1, interval_minutes=30)
        if payload.enabled is not None:
            p.enabled = payload.enabled
        if payload.interval_minutes is not None:
            p.interval_minutes = max(1, payload.interval_minutes)
        p.next_run_time = (p.last_run_time or now) + timedelta(minutes=p.interval_minutes or 30)
        interval = p.interval_minutes
    else:
        raise HTTPException(400, f"未知任务类型: {task_type}")

    db.commit()
    return {"status": "ok", "task_type": task_type, "instance_id": instance_id,
            "enabled": p.enabled, "interval_minutes": interval, "next_run_time": _iso(p.next_run_time)}


@router.post("/tasks/{task_type}/{instance_id}/run-now")
def api_run_now(task_type: str, instance_id: int, request: Request, db: Session = Depends(get_db)):
    _require_instance_access(request, db, instance_id, "operate")
    inst = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not inst:
        raise HTTPException(404, "实例不存在")
    try:
        if task_type == "capacity":
            from app.services.capacity_monitor import run_collect_by_policy

            policy = _get_or_create(db, DBCapacityPolicy, instance_id)
            db.commit()
            db.refresh(policy)
            return run_collect_by_policy(db, policy)
        elif task_type == "awr":
            from app.services.awr_collect import run_collect_by_policy

            policy = _get_or_create(db, DBAwrCollectPolicy, instance_id)
            db.commit()
            db.refresh(policy)
            return run_collect_by_policy(db, policy)
        elif task_type == "perf_baseline":
            from app.services.perf_baseline import run_collect_by_policy

            policy = _get_or_create(db, DBPerfMetricPolicy, instance_id, enabled=1, interval_minutes=30)
            db.commit()
            db.refresh(policy)
            return run_collect_by_policy(db, policy)
        else:
            raise HTTPException(400, f"未知任务类型: {task_type}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("立即执行失败 | %s/%s | %s", task_type, instance_id, e, exc_info=True)
        raise HTTPException(500, f"执行失败：{e}")
