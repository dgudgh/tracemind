import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.core.security import password_complexity_issues
from app.services.sqlite_init_service import get_sqlite_db_path, is_sqlite_mode, is_initialized, init_sqlite_database

logger = logging.getLogger("app.system_init")
router = APIRouter(prefix="/system/init", tags=["system-init"])


class SetupRequest(BaseModel):
    admin_username: str = Field(..., min_length=1, max_length=80)
    admin_password: str = Field(..., min_length=8, max_length=256)


class SetupResponse(BaseModel):
    status: str
    message: str
    db_type: str = ""
    database_file: str = ""


@router.get("/status")
def get_init_status():
    from app.core.config import settings
    initialized = False
    if is_sqlite_mode():
        initialized = is_initialized()
        return {
            "initialized": initialized,
            "app_mode": settings.APP_MODE,
            "db_type": "sqlite" if settings.is_sqlite else "mysql",
            "database_file": get_sqlite_db_path() if settings.is_sqlite else "",
        }
    try:
        from app.api.v1.setup import is_configured
        initialized = is_configured()
    except Exception as exc:
        logger.warning("setup configured status check failed: %s", exc)
        initialized = True
    return {
        "initialized": initialized,
        "app_mode": settings.APP_MODE,
        "db_type": "mysql",
        "database_file": "",
    }


@router.post("/setup")
def execute_setup(req: SetupRequest):
    from app.core.config import settings

    if not is_sqlite_mode():
        return SetupResponse(status="error", message="当前模式为 MySQL，不支持此接口初始化")

    if is_initialized():
        return SetupResponse(status="error", message="系统已初始化，不可重复执行")

    issues = password_complexity_issues(req.admin_password)
    if issues:
        raise HTTPException(status_code=400, detail="；".join(issues))

    try:
        init_sqlite_database(req.admin_username, req.admin_password)
        return SetupResponse(
            status="success",
            message="SQLite 测试库初始化完成",
            db_type="sqlite",
            database_file=get_sqlite_db_path(),
        )
    except Exception as e:
        logger.error("Init failed: %s", e)
        return SetupResponse(status="error", message=f"初始化失败: {e}")
