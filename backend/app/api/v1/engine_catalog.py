"""Authenticated read-only engine catalog; package lifecycle is host-only."""
import importlib.util

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from uuid import uuid4

from app.api.v1.assistant import _actor
from app.core.database import get_db
from app.engine_registry import Catalog
from app.runtime.engine_sdk.profiles import PROFILES
from app.services import capability_registry
from app.services.engine_catalog_view import catalog_view

_EMPTY_BOOT_ID = uuid4().hex

# 驱动候选：按 connector 的真实回退行为列，不按 PROFILES 里的单一声明。
# postgresql_connection 先试 psycopg2、失败再试 psycopg(v3)，所以两个都算可用。
# 这一列**只报告本机是否可导入**，不表示对应引擎已完成服务端认证。
_DRIVER_CANDIDATES = {
    "mysql": ("pymysql",),
    "oracle": ("oracledb",),
    "postgresql": ("psycopg2", "psycopg"),
    "sqlserver": ("pymssql",),
    "dm": ("dmPython",),
    "kingbasees": ("ksycopg2",),
}

router = APIRouter(prefix="/engine-catalog", tags=["engine-catalog"])


def _module_present(name: str) -> bool:
    """只查模块是否可被找到，不 import —— 避免在请求路径上产生驱动副作用。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def engine_driver_status() -> dict:
    rows = []
    for engine_id, profile in PROFILES.items():
        declared = str(profile.get("driver") or "")
        candidates = _DRIVER_CANDIDATES.get(engine_id) or ((declared,) if declared else ())
        found = [name for name in candidates if _module_present(name)]
        rows.append({
            "engine_id": engine_id,
            "declared_driver": declared,
            "candidates": list(candidates),
            "installed": bool(found),
            "available_via": found,
            "note": None if found else "驱动未安装；该引擎当前无法建立真实连接",
        })
    return {
        "schema": "engine.driver_status/v1",
        "scope": "host_import_only",
        # 明确边界：本机可导入 ≠ 引擎包已安装 ≠ 服务端已认证。
        "disclaimer": "仅反映本机 Python 环境是否可导入驱动，不代表引擎包已安装或服务端已认证",
        "drivers": rows,
    }


@router.get("/drivers")
def read_engine_drivers(actor: dict = Depends(_actor)):
    return engine_driver_status()


@router.get("")
def read_engine_catalog(request: Request, response: Response, actor: dict = Depends(_actor), db: Session = Depends(get_db)):
    if request.query_params:
        raise HTTPException(status_code=400, detail={"code": "invalid_query"})
    # Capture one immutable snapshot before projecting any fields.
    publisher = getattr(request.app.state, "engine_catalog_publisher", None)
    try:
        catalog = publisher.refresh() if publisher is not None else Catalog.empty()
        visible = capability_registry.list_capabilities(db, actor=actor)
        authorized = {}
        for item in visible["items"]:
            if item.get("availability") == "available" and item.get("plan_executable"):
                for engine in item.get("authorized_engines", []):
                    authorized.setdefault(engine, set()).add(item["capability_id"])
        result = catalog_view(catalog, authorized, publisher.boot_id if publisher else _EMPTY_BOOT_ID)
    except capability_registry.CapabilityError as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "catalog_unavailable"}) from exc
    response.headers["Cache-Control"] = "no-store, private"
    return result
