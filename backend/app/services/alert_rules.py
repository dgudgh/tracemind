from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.alert_rule import DBAlertRule

logger = logging.getLogger("app.services.alert_rules")

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "default_alert_rules.json"


def _rule_to_dict(rule: DBAlertRule) -> dict:
    return {
        "id": rule.id,
        "rule_name": rule.rule_name,
        "rule_type": rule.rule_type,
        "metric_name": rule.metric_name,
        "level": rule.level,
        "consecutive_times": rule.consecutive_times,
        "operator": rule.operator,
        "threshold": rule.threshold,
        "description": rule.description,
        "enabled": rule.enabled,
        "source": rule.source,
        "create_time": rule.create_time.isoformat(sep=" ") if rule.create_time else None,
        "update_time": rule.update_time.isoformat(sep=" ") if rule.update_time else None,
    }


def ensure_default_rules(db: Session) -> int:
    if db.query(DBAlertRule).count() > 0:
        return 0
    if not DEFAULT_RULES_PATH.exists():
        logger.warning("默认告警规则文件不存在 | path=%s", DEFAULT_RULES_PATH)
        return 0

    rows = json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    seen: set[tuple[str, str, str]] = set()
    created_count = 0
    for row in rows:
        key = (row["rule_name"], row["metric_name"], row["level"])
        if key in seen:
            continue
        seen.add(key)
        db.add(
            DBAlertRule(
                rule_name=row["rule_name"],
                rule_type=row.get("rule_type"),
                metric_name=row["metric_name"],
                level=row["level"],
                consecutive_times=row.get("consecutive_times") or 1,
                operator=row.get("operator"),
                threshold=row.get("threshold"),
                description=row.get("description"),
                enabled=1,
                source="告警规则.xlsx",
            )
        )
        created_count += 1
    db.commit()
    logger.info("默认告警规则导入完成 | count=%s", created_count)
    return created_count


def list_rules(
    db: Session,
    rule_type: str | None = None,
    level: str | None = None,
    enabled: int | None = None,
    keyword: str | None = None,
) -> list[dict]:
    query = db.query(DBAlertRule)
    if rule_type:
        query = query.filter(DBAlertRule.rule_type.like(f"%{rule_type}%"))
    if level:
        query = query.filter(DBAlertRule.level == level)
    if enabled is not None:
        query = query.filter(DBAlertRule.enabled == enabled)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.filter(
            DBAlertRule.rule_name.like(like)
            | DBAlertRule.metric_name.like(like)
            | DBAlertRule.description.like(like)
        )
    rows = query.order_by(DBAlertRule.rule_type.asc(), DBAlertRule.rule_name.asc(), DBAlertRule.level.asc()).all()
    return [_rule_to_dict(row) for row in rows]


def get_rule_stats(db: Session) -> dict:
    total = db.query(DBAlertRule).count()
    enabled = db.query(DBAlertRule).filter(DBAlertRule.enabled == 1).count()
    critical = db.query(DBAlertRule).filter(DBAlertRule.level == "critical", DBAlertRule.enabled == 1).count()
    warning = db.query(DBAlertRule).filter(DBAlertRule.level == "warning", DBAlertRule.enabled == 1).count()
    types = sorted({row[0] for row in db.query(DBAlertRule.rule_type).distinct().all() if row[0]})
    return {
        "total": total,
        "enabled": enabled,
        "disabled": total - enabled,
        "critical": critical,
        "warning": warning,
        "types": types,
    }


def update_rule_enabled(db: Session, rule_id: int, enabled: int) -> dict | None:
    rule = db.query(DBAlertRule).filter(DBAlertRule.id == rule_id).first()
    if not rule:
        return None
    rule.enabled = 1 if enabled else 0
    db.commit()
    db.refresh(rule)
    return _rule_to_dict(rule)
