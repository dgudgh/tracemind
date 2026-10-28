from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.models.capacity_policy import DBCapacityAlert
from app.schemas.capacity import CapacityPolicyUpdate
from app.services import alert_rules
from app.services import capacity_monitor
from app.services import instances as instance_service
from app.services.access_control import resolve_scope
from app.tasks.capacity_scheduler import get_all_policies_status
from app.tasks.master_scheduler import get_scheduler_status

router = APIRouter(prefix="/capacity", tags=["容量预测"])


def _parse_datetime_filter(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            parsed = datetime.fromisoformat(text)
            return datetime.combine(parsed.date(), time.max if end_of_day else time.min)
        return datetime.fromisoformat(text.replace("T", " "))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式无效: {value}")


@router.get("/scheduler/status")
def scheduler_status():
    return get_scheduler_status()


def _request_auth(request: Request) -> dict:
    return getattr(request.state, "auth", {}) or {}


def _scope_native_ids(request: Request, db: Session, minimum_access: str = "read") -> frozenset[int] | None:
    auth = _request_auth(request)
    if auth.get("role") == "admin" or auth.get("source") not in {"db", "identity"}:
        return None
    user_id = int(auth.get("user_id") or 0)
    if not user_id:
        return None
    scope = resolve_scope(db, user_id, minimum_access=minimum_access)
    return scope.native_instance_ids if scope.restricted else None


@router.get("/policies")
def all_policies(request: Request, db: Session = Depends(get_db)):
    allowed = _scope_native_ids(request, db)
    items = get_all_policies_status(db)
    if allowed is not None:
        items = [item for item in items if item.get("instance_id") in allowed]
    return {"items": items}


def _get_instance(
    request: Request,
    db: Session,
    instance_id: int,
    minimum_access: str = "read",
) -> DBInstance:
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="数据库实例不存在")
    allowed = _scope_native_ids(request, db, minimum_access=minimum_access)
    if allowed is not None and instance_id not in allowed:
        raise HTTPException(status_code=404, detail="数据库实例不存在或不在当前数据范围内")
    return instance


def _get_alert(
    request: Request,
    db: Session,
    alert_id: int,
    minimum_access: str = "read",
) -> DBCapacityAlert:
    alert = db.query(DBCapacityAlert).filter(DBCapacityAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    allowed = _scope_native_ids(request, db, minimum_access=minimum_access)
    if allowed is not None and alert.instance_id not in allowed:
        raise HTTPException(status_code=404, detail="告警不存在或不在当前数据范围内")
    return alert


@router.get("/policies/{instance_id}")
def get_capacity_policy(instance_id: int, request: Request, db: Session = Depends(get_db)):
    _get_instance(request, db, instance_id)
    return capacity_monitor.policy_to_dict(capacity_monitor.get_or_create_policy(db, instance_id))


@router.put("/policies/{instance_id}")
def update_capacity_policy(instance_id: int, payload: CapacityPolicyUpdate, request: Request, db: Session = Depends(get_db)):
    _get_instance(request, db, instance_id, minimum_access="operate")
    return capacity_monitor.policy_to_dict(capacity_monitor.update_policy(db, instance_id, payload))


@router.delete("/policies/{instance_id}")
def delete_capacity_policy(instance_id: int, request: Request, db: Session = Depends(get_db)):
    _get_instance(request, db, instance_id, minimum_access="operate")
    capacity_monitor.delete_policy(db, instance_id)
    return {"detail": "策略已删除"}


@router.post("/{instance_id}/collect")
def collect_capacity_snapshot(instance_id: int, request: Request, db: Session = Depends(get_db)):
    instance = _get_instance(request, db, instance_id, minimum_access="operate")
    return capacity_monitor.collect_snapshot(db, instance)


@router.get("/{instance_id}/summary")
def capacity_summary(instance_id: int, request: Request, db: Session = Depends(get_db)):
    instance = _get_instance(request, db, instance_id)
    return capacity_monitor.latest_summary(db, instance)


@router.get("/{instance_id}/analysis")
def capacity_analysis(instance_id: int, request: Request, days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    instance = _get_instance(request, db, instance_id)
    return capacity_monitor.analyze_capacity(db, instance, days)


@router.get("/{instance_id}/snapshots")
def capacity_snapshots(instance_id: int, request: Request, days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    _get_instance(request, db, instance_id)
    return {"items": capacity_monitor.list_snapshots(db, instance_id, days)}


@router.get("/alerts")
def capacity_alerts(
    request: Request,
    instance_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("open", pattern="^(open|closed|isolated|all)$"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return {
        "items": capacity_monitor.list_alerts(
            db,
            instance_id,
            limit,
            status,
            date_from=_parse_datetime_filter(date_from),
            date_to=_parse_datetime_filter(date_to, end_of_day=True),
            allowed_instance_ids=_scope_native_ids(request, db),
        )
    }


@router.put("/alerts/{alert_id}/close")
def close_capacity_alert(alert_id: int, request: Request, db: Session = Depends(get_db)):
    _get_alert(request, db, alert_id, minimum_access="operate")
    result = capacity_monitor.close_alert(db, alert_id)
    if not result:
        raise HTTPException(status_code=404, detail="告警不存在")
    return result


@router.put("/alerts/{alert_id}/isolate")
def isolate_capacity_alert(
    alert_id: int,
    request: Request,
    days: int = Query(..., ge=1, le=365),
    reason: str | None = Query(None, max_length=500),
    db: Session = Depends(get_db),
):
    _get_alert(request, db, alert_id, minimum_access="operate")
    result = capacity_monitor.isolate_alert(db, alert_id, days, reason)
    if not result:
        raise HTTPException(status_code=404, detail="告警不存在")
    return result


@router.put("/alerts/close-all")
def close_all_capacity_alerts(
    request: Request,
    instance_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    if instance_id is not None:
        _get_instance(request, db, instance_id, minimum_access="operate")
    count = capacity_monitor.close_all_alerts(
        db,
        instance_id,
        date_from=_parse_datetime_filter(date_from),
        date_to=_parse_datetime_filter(date_to, end_of_day=True),
        allowed_instance_ids=_scope_native_ids(request, db, minimum_access="operate"),
    )
    return {"closed_count": count}


@router.get("/alerts/stats")
def alert_stats(
    request: Request,
    instance_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return capacity_monitor.get_alert_stats(
        db,
        instance_id,
        date_from=_parse_datetime_filter(date_from),
        date_to=_parse_datetime_filter(date_to, end_of_day=True),
        allowed_instance_ids=_scope_native_ids(request, db),
    )


@router.get("/alert-rules")
def list_alert_rules(
    rule_type: str | None = Query(None),
    level: str | None = Query(None, pattern="^(warning|critical)$"),
    enabled: int | None = Query(None, ge=0, le=1),
    keyword: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return {"items": alert_rules.list_rules(db, rule_type=rule_type, level=level, enabled=enabled, keyword=keyword)}


@router.get("/alert-rules/stats")
def alert_rule_stats(db: Session = Depends(get_db)):
    return alert_rules.get_rule_stats(db)


@router.put("/alert-rules/{rule_id}/enabled")
def update_alert_rule_enabled(rule_id: int, enabled: int = Query(..., ge=0, le=1), db: Session = Depends(get_db)):
    result = alert_rules.update_rule_enabled(db, rule_id, enabled)
    if not result:
        raise HTTPException(status_code=404, detail="告警规则不存在")
    return result


@router.get("/trend")
def capacity_trend(request: Request, instance_id: int | None = Query(None), days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    if not instance_id:
        return {"items": [], "message": "请选择实例查看容量趋势。"}
    instance = _get_instance(request, db, instance_id)
    return capacity_monitor.analyze_capacity(db, instance, days)
