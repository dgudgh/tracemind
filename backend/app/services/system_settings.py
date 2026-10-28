"""系统参数服务 — 通用键值配置，带类型化读取与默认值目录。

新增参数：只需在 DEFAULTS 增加一条；ensure_defaults 启动时补齐。
代码里读取：settings_service.get_int("slow_sql_critical_ms") 等。
"""
import logging

from sqlalchemy.orm import Session

from app.models.system_setting import DBSystemSetting

logger = logging.getLogger("app.services.system_settings")

# key -> (default, type, category, label, description)
DEFAULTS = {
    # 慢SQL阈值（毫秒）
    "slow_sql_threshold_ms": (3000, "int", "slow_sql", "慢SQL判定阈值(ms)", "平均耗时超过该值视为慢SQL并入库"),
    "slow_sql_warning_ms": (1000, "int", "slow_sql", "警告阈值(ms)", "平均耗时超过该值标记为 warning"),
    "slow_sql_critical_ms": (5000, "int", "slow_sql", "严重阈值(ms)", "平均耗时超过该值标记为 critical"),
    # 数据保留天数
    "retention_slow_sql_days": (1, "int", "retention", "慢SQL记录保留(天)", "超过天数的慢SQL记录自动清理"),
    "retention_perf_metric_days": (90, "int", "retention", "性能指标保留(天)", "性能基线指标快照保留天数"),
    # 平台基础信息
    "platform_name": ("TraceMind AI 智能运维平台", "str", "platform", "平台名称", "页面标题/页脚显示的平台名称"),
    "platform_footer": ("TraceMind AI 智能运维平台", "str", "platform", "页脚标识", "登录页/页脚显示文案"),
    # 告警默认阈值（新建容量策略时的全局默认）
    "alert_forecast_days_threshold": (15, "int", "alert", "容量预测预警天数", "预测可用天数低于该值触发告警(新策略默认)"),
    "alert_abnormal_growth_pct": (30, "int", "alert", "异常增长百分比", "增长超过该百分比视为异常(新策略默认)"),
    # 登录与密码策略
    "password_expire_days": (90, "int", "security", "密码有效期(天)", "超过有效期后禁止创建完整登录会话"),
    "max_login_fail": (5, "int", "security", "最大登录失败次数", "连续失败达到该次数后临时锁定账号"),
    "lock_duration_minutes": (30, "int", "security", "账号锁定时长(分钟)", "临时锁定到期后自动恢复"),
    "enforce_password_complexity": (True, "bool", "security", "强制密码复杂度", "要求密码包含大小写字母和数字"),
}

_TYPE_CAST = {
    "int": lambda v: int(float(v)),
    "float": lambda v: float(v),
    "bool": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "str": lambda v: str(v),
}


def ensure_defaults(db: Session) -> int:
    existing = {s.key for s in db.query(DBSystemSetting).all()}
    added = 0
    for key, (default, vtype, cat, label, desc) in DEFAULTS.items():
        if key in existing:
            continue
        db.add(DBSystemSetting(key=key, value=str(default), value_type=vtype,
                               category=cat, label=label, description=desc))
        added += 1
    if added:
        db.commit()
    return added


def _cast(raw, vtype, fallback):
    try:
        return _TYPE_CAST.get(vtype, str)(raw)
    except (TypeError, ValueError):
        return fallback


def get(db: Session, key: str):
    meta = DEFAULTS.get(key)
    default = meta[0] if meta else None
    vtype = meta[1] if meta else "str"
    row = db.query(DBSystemSetting).filter(DBSystemSetting.key == key).first()
    if not row or row.value is None:
        return default
    return _cast(row.value, row.value_type or vtype, default)


def get_int(db: Session, key: str, fallback: int = 0) -> int:
    v = get(db, key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


def get_str(db: Session, key: str, fallback: str = "") -> str:
    v = get(db, key)
    return str(v) if v is not None else fallback


def list_settings(db: Session) -> list[dict]:
    ensure_defaults(db)
    rows = {s.key: s for s in db.query(DBSystemSetting).all()}
    out = []
    for key, (default, vtype, cat, label, desc) in DEFAULTS.items():
        row = rows.get(key)
        out.append({
            "key": key,
            "value": (row.value if row and row.value is not None else str(default)),
            "value_type": vtype,
            "category": cat,
            "label": label,
            "description": desc,
            "default": str(default),
        })
    return out


def update_settings(db: Session, updates: dict) -> int:
    n = 0
    for key, value in (updates or {}).items():
        if key not in DEFAULTS:
            continue
        vtype = DEFAULTS[key][1]
        # 校验可转换
        _ = _cast(value, vtype, None)
        row = db.query(DBSystemSetting).filter(DBSystemSetting.key == key).first()
        if not row:
            meta = DEFAULTS[key]
            row = DBSystemSetting(key=key, value_type=vtype, category=meta[2], label=meta[3], description=meta[4])
            db.add(row)
        row.value = str(value)
        n += 1
    db.commit()
    return n
