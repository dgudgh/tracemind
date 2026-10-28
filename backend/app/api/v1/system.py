from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.services import operations_dashboard
from app.services.access_control import resolve_scope

router = APIRouter(prefix="/system", tags=["系统配置"])


@router.get("/config")
def get_system_config(db: Session = Depends(get_db)):
    from app.services import system_settings as settings_svc
    return {
        "project_name": settings.PROJECT_NAME,
        "platform_name": settings_svc.get_str(db, "platform_name", settings.PROJECT_NAME),
        "platform_footer": settings_svc.get_str(db, "platform_footer", ""),
        "version": settings.VERSION,
        "api_prefix": settings.API_V1_PREFIX,
    }


@router.get("/api-modules")
def get_api_modules():
    from app.api.v1 import get_api_module_catalog

    return {"items": get_api_module_catalog()}


@router.get("/dashboard-stats")
def get_dashboard_stats(request: Request, db: Session = Depends(get_db)):
    auth = getattr(request.state, "auth", {}) or {}
    allowed_instance_ids = None
    if auth.get("role") != "admin" and auth.get("source") in {"db", "identity"}:
        user_id = int(auth.get("user_id") or 0)
        if user_id:
            scope = resolve_scope(db, user_id)
            if scope.restricted:
                allowed_instance_ids = set(scope.native_instance_ids)
    return operations_dashboard.build_operations_dashboard(
        db,
        allowed_instance_ids=allowed_instance_ids,
        username=auth.get("username") or auth.get("subject"),
    )
