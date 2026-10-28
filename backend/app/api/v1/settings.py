import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import system_settings as svc

logger = logging.getLogger("app.api.settings")
router = APIRouter(prefix="/settings", tags=["系统参数"])

CATEGORY_LABELS = {
    "slow_sql": "慢 SQL 阈值",
    "retention": "数据保留",
    "platform": "平台信息",
    "alert": "告警默认阈值",
    "general": "通用",
}


class SettingsUpdate(BaseModel):
    updates: dict


@router.get("")
def api_get_settings(db: Session = Depends(get_db)):
    items = svc.list_settings(db)
    # 按分类分组，便于前端表单渲染
    groups: dict[str, dict] = {}
    for it in items:
        cat = it["category"]
        groups.setdefault(cat, {"category": cat, "label": CATEGORY_LABELS.get(cat, cat), "items": []})
        groups[cat]["items"].append(it)
    return {"groups": list(groups.values()), "items": items}


@router.put("")
def api_update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    n = svc.update_settings(db, payload.updates or {})
    return {"status": "ok", "updated": n}
