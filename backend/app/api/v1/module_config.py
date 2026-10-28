import fastapi
import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.module_config import ModuleConfig
from app.models.user import UserModulePermission

logger = logging.getLogger("app.api.module_config")

router = APIRouter(prefix="/modules", tags=["模块配置"])

DEFAULT_MODULES = [
    {"module_key": "home.dashboard", "module_name": "工作台", "category": "overview", "sort_order": 1},
    {"module_key": "asset.center", "module_name": "资产管理", "category": "assets", "sort_order": 2},
    {"module_key": "monitor.alert.unified", "module_name": "综合告警", "category": "monitoring", "sort_order": 4},
    {"module_key": "monitor.alert.db", "module_name": "容量告警", "category": "monitoring", "sort_order": 5},
    {"module_key": "monitor.observability", "module_name": "可观测监控", "category": "monitoring", "sort_order": 6},
    {"module_key": "inspect.overview", "module_name": "巡检总览", "category": "inspection", "sort_order": 7},
    {"module_key": "inspect.run", "module_name": "一键巡检", "category": "inspection", "sort_order": 8},
    {"module_key": "inspect.tasks", "module_name": "巡检任务", "category": "inspection", "sort_order": 9},
    {"module_key": "inspect.reports", "module_name": "巡检报告", "category": "inspection", "sort_order": 10},
    {"module_key": "perf.slowsql", "module_name": "慢 SQL 分析", "category": "performance", "sort_order": 16},
    {"module_key": "perf.lock", "module_name": "锁等待分析", "category": "performance", "sort_order": 17},
    {"module_key": "perf.baseline", "module_name": "性能基线", "category": "performance", "sort_order": 20},
    {"module_key": "perf.capacity", "module_name": "容量分析", "category": "performance", "sort_order": 22},
    {"module_key": "perf.awr.report", "module_name": "AWR 综合（导出/上传）", "category": "performance", "sort_order": 24},
    {"module_key": "diagnose.smart", "module_name": "智能诊断", "category": "ai", "sort_order": 28},
    {"module_key": "ops.configGovernance", "module_name": "配置基线", "category": "operations", "sort_order": 31},
    {"module_key": "ops.overview", "module_name": "运行概览", "category": "operations", "sort_order": 31},
    {"module_key": "ops.space", "module_name": "空间管理", "category": "operations", "sort_order": 32},
    {"module_key": "ops.log", "module_name": "日志管理", "category": "operations", "sort_order": 33},
    {"module_key": "ops.session", "module_name": "会话事务", "category": "operations", "sort_order": 34},
    {"module_key": "ops.object", "module_name": "对象统计", "category": "operations", "sort_order": 35},
    {"module_key": "ops.security", "module_name": "用户安全", "category": "operations", "sort_order": 36},
    {"module_key": "report.unified", "module_name": "统一报告", "category": "reports", "sort_order": 48},
    {"module_key": "report.alert", "module_name": "告警报告", "category": "reports", "sort_order": 49},
    {"module_key": "system.user", "module_name": "用户管理", "category": "settings", "sort_order": 55},
    {"module_key": "system.params", "module_name": "系统参数", "category": "settings", "sort_order": 56},
    {"module_key": "platform.engines", "module_name": "引擎与版本", "category": "settings", "sort_order": 57},
    {"module_key": "system.ai", "module_name": "AI 配置", "category": "settings", "sort_order": 57},
    {"module_key": "system.schedule", "module_name": "调度任务", "category": "settings", "sort_order": 58},
    {"module_key": "moduleConfig", "module_name": "权限配置", "category": "settings", "sort_order": 59},
]

for _sort_order, _module in enumerate(DEFAULT_MODULES, start=1):
    _module["sort_order"] = _sort_order

MODULE_KEY_ALIASES = {
    "asset.instances": "asset.center",
    "asset.hosts": "asset.center",
    "asset.cmdb": "asset.center",
    "diagnose.ai": "diagnose.smart",
    "diagnose.rule": "diagnose.smart",
}


def _migrate_module_permissions(db: Session):
    legacy_rows = (
        db.query(UserModulePermission)
        .filter(UserModulePermission.module_key.in_(MODULE_KEY_ALIASES))
        .all()
    )
    for row in legacy_rows:
        target_key = MODULE_KEY_ALIASES[row.module_key]
        duplicate = (
            db.query(UserModulePermission)
            .filter(
                UserModulePermission.user_id == row.user_id,
                UserModulePermission.module_key == target_key,
                UserModulePermission.id != row.id,
            )
            .first()
        )
        if duplicate:
            db.delete(row)
        else:
            row.module_key = target_key
    if legacy_rows:
        db.commit()
        logger.info("迁移旧模块权限 (%d 项)", len(legacy_rows))


def _ensure_defaults(db: Session):
    _migrate_module_permissions(db)
    existing = {r.module_key: r for r in db.query(ModuleConfig).all()}
    default_keys = {m["module_key"] for m in DEFAULT_MODULES}

    new_items = [m for m in DEFAULT_MODULES if m["module_key"] not in existing]
    if new_items:
        for m in new_items:
            db.add(ModuleConfig(**m, enabled=1))
        db.commit()
        logger.info("补全模块配置 (%d 项)", len(new_items))

    for m in DEFAULT_MODULES:
        if m["module_key"] in existing:
            row = existing[m["module_key"]]
            row.module_name = m["module_name"]
            row.category = m["category"]
            row.sort_order = m["sort_order"]
    db.commit()

    stale_keys = set(existing.keys()) - default_keys
    if stale_keys:
        for key in stale_keys:
            db.delete(existing[key])
        db.commit()
        logger.info("清理过期模块配置 (%d 项): %s", len(stale_keys), stale_keys)

    if not existing:
        for m in DEFAULT_MODULES:
            if m["module_key"] not in set(n["module_key"] for n in new_items):
                db.add(ModuleConfig(**m, enabled=1))
        db.commit()
        logger.info("初始化默认模块配置 (%d 项)", len(DEFAULT_MODULES))


@router.get("")
def list_modules(request: fastapi.Request, db: Session = Depends(get_db)):
    try:
        _ensure_defaults(db)
    except Exception as e:
        logger.warning("_ensure_defaults failed: %s", e)
        try:
            db.rollback()
        except Exception as rollback_error:
            logger.warning("module config rollback failed: %s", rollback_error)
    rows = db.query(ModuleConfig).order_by(ModuleConfig.sort_order).all()
    all_items = [
        {"id": r.id, "module_key": r.module_key, "module_name": r.module_name,
         "category": r.category, "enabled": r.enabled, "sort_order": r.sort_order}
        for r in rows
    ]
    # 开源版：企业版模块一律按"未启用"下发，前端据此隐藏菜单。
    # 与 app/core/edition.py 的接口门禁共用同一份清单，避免"菜单藏了但接口能调"。
    from app.core import edition
    if edition.BASIC_ONLY:
        all_items = [
            {**m, "enabled": m["enabled"] if edition.module_allowed(m["module_key"]) else 0}
            for m in all_items
        ]
    info = getattr(request.state, "auth", None)
    if request.query_params.get("scope") == "all" and info and info.get("role") == "admin":
        return {"items": all_items}
    if info and info.get("role") == "admin":
        return {"items": all_items}
    if info and info.get("role") != "admin":
        all_items = [m for m in all_items if m["module_key"] not in ("userManagement", "moduleConfig")]
    if info:
        from app.models.user import User, UserModulePermission
        user_id = info.get("user_id")
        if not user_id and info.get("username") == "admin":
            admin = db.query(User).filter(User.username == "admin").first()
            user_id = admin.id if admin else None
        if user_id:
            perms = db.query(UserModulePermission).filter(UserModulePermission.user_id == user_id).all()
            allowed = {p.module_key for p in perms}
            if info.get("role") == "admin" and not allowed:
                return {"items": all_items}
            all_items = [m for m in all_items if m["module_key"] in allowed]
    return {"items": all_items}


class ModuleUpdate(BaseModel):
    enabled: int | None = None


@router.put("/{module_key}")
def update_module(module_key: str, body: ModuleUpdate, db: Session = Depends(get_db)):
    # 开源版不允许把企业版模块打开：否则界面能藏、开关能开，版本边界形同虚设。
    from app.core import edition
    if edition.BASIC_ONLY and not edition.module_allowed(module_key):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=edition.denial(module_key))
    row = db.query(ModuleConfig).filter(ModuleConfig.module_key == module_key).first()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="模块不存在")
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return {"ok": True, "module_key": module_key, "enabled": row.enabled}


class BatchUpdate(BaseModel):
    modules: list[dict]


@router.put("")
def batch_update(body: BatchUpdate, db: Session = Depends(get_db)):
    for item in body.modules:
        key = item.get("module_key")
        enabled = item.get("enabled")
        if key is None or enabled is None:
            continue
        row = db.query(ModuleConfig).filter(ModuleConfig.module_key == key).first()
        if row:
            row.enabled = enabled
    db.commit()
    return {"ok": True, "updated": len(body.modules)}


@router.post("/reset")
def reset_modules(db: Session = Depends(get_db)):
    db.query(ModuleConfig).delete()
    db.commit()
    _ensure_defaults(db)
    return {"ok": True, "message": "已重置为默认配置"}
