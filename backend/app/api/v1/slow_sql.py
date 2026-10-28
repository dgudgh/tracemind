import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.database_options import redact_database_error
from app.schemas.common import PageResponse
from app.schemas.slow_sql import SlowSQLResponse, SlowSQLAIRequest, OracleSQLAIRequest
from app.services import slow_sql as slow_sql_service
from app.services import slow_sql_ai
from app.services.access_control import scope_allows_entity

logger = logging.getLogger("app.api.slow_sql")
router = APIRouter(prefix="/slow-sql", tags=["慢 SQL 分析"])


@router.get("", response_model=PageResponse)
def list_slow_sql(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    instance_id: int | None = Query(None),
    db_type: str | None = Query(None, pattern="^(oracle|mysql|postgresql|sqlserver)$"),
    risk_level: str | None = Query(None),
    db: Session = Depends(get_db),
):
    items, total = slow_sql_service.get_list(db, instance_id, db_type, risk_level, page, page_size)
    return PageResponse(items=[SlowSQLResponse.model_validate(item) for item in items], total=total, page=page, page_size=page_size)


def _instance_access(request: Request, db: Session, instance_id: int, minimum_access="read"):
    from app.models.db_instance import DBInstance

    auth = getattr(request.state, "auth", {}) or {}
    if not auth.get("user_id"):
        raise HTTPException(401, "请先登录")
    if auth.get("role") != "admin" and not scope_allows_entity(db, int(auth["user_id"]), "native_instance", instance_id, minimum_access):
        raise HTTPException(403, "实例不在当前账号的授权范围内")
    instance = db.get(DBInstance, instance_id)
    if not instance:
        raise HTTPException(404, "实例不存在")
    return instance, str(auth.get("username") or auth["user_id"])


def _analysis_response(callback):
    try:
        result = callback()
    except slow_sql_ai.AnalysisError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if result.get("status") == "failed":
        return JSONResponse(status_code=502, content={**result, "message": result["error"]})
    return result


@router.get("/ai-capability")
def slow_sql_ai_capability(db: Session = Depends(get_db)):
    return slow_sql_ai.capability(db)


@router.get("/analysis-reports")
def slow_sql_analysis_reports(request: Request, instance_id: int = Query(..., gt=0),
                              sql_id: str | None = Query(None, max_length=100),
                              limit: int = Query(20, ge=1, le=50), db: Session = Depends(get_db)):
    _instance_access(request, db, instance_id)
    return {"items": slow_sql_ai.list_reports(db, instance_id, sql_id, limit)}


@router.post("/oracle/{instance_id}/analyze")
def analyze_oracle_sql(instance_id: int, payload: OracleSQLAIRequest, request: Request, db: Session = Depends(get_db)):
    instance, actor = _instance_access(request, db, instance_id, "operate")
    if instance.db_type != "oracle":
        raise HTTPException(422, "此入口只接受 Oracle 实例")
    def execute():
        provider = slow_sql_ai.resolve_provider(db, payload)
        evidence = slow_sql_ai.oracle_evidence(instance, payload)
        return slow_sql_ai.run_analysis(db, instance=instance, provider=provider, evidence=evidence, actor=actor)
    return _analysis_response(execute)


@router.post("/{slow_sql_id}/analyze")
def analyze_slow_sql(slow_sql_id: int, payload: SlowSQLAIRequest, request: Request, db: Session = Depends(get_db)):
    obj = slow_sql_service.get_by_id(db, slow_sql_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Slow SQL not found")
    instance, actor = _instance_access(request, db, obj.instance_id, "operate")
    def execute():
        provider = slow_sql_ai.resolve_provider(db, payload)
        return slow_sql_ai.run_analysis(db, instance=instance, provider=provider,
                                       evidence=slow_sql_ai.stored_evidence(obj), actor=actor)
    return _analysis_response(execute)


@router.post("/collect/{instance_id}")
def collect_slow_sql(instance_id: int, hours: int = Query(1, ge=1, le=168), db: Session = Depends(get_db)):
    from app.models.db_instance import DBInstance

    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(404, "实例不存在")

    if instance.db_type not in {"oracle", "mysql"}:
        raise HTTPException(400, f"不支持的数据库类型: {instance.db_type}")
    try:
        return slow_sql_service.collect(db, instance, hours=hours)
    except slow_sql_service.SlowSQLCollectionError as exc:
        raise HTTPException(502, f"采集失败，历史保留：{redact_database_error(exc)}") from exc
    except Exception as exc:
        logger.warning("Slow SQL collection failed | instance_id=%s | error=%s", instance_id, redact_database_error(exc))
        raise HTTPException(500, "采集未完成，历史记录未删除，请检查服务日志。") from exc
