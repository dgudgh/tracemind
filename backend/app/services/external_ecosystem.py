from sqlalchemy.orm import Session

from app.core.crypto import encrypt_password
from app.models.external_ecosystem import (
    EcosystemIntegrationConfig,
    EcosystemIntegrationEvent,
    EcosystemIntegrationPlan,
)
from app.services.ecosystem_registry import (
    CAPABILITY_REGISTRY,
    get_capability,
    summarize_capabilities,
)


# Domain ownership lives in ecosystem_registry. Keep legacy configuration and
# history compatibility here; domain actions use their dedicated services.
TASKS = list(CAPABILITY_REGISTRY.values())
LEGACY_MIGRATION_VERSION = "ecosystem-domain-ownership-v1"


def owner_for_task(task_code: str) -> dict | None:
    capability = get_capability(task_code)
    if not capability:
        return None
    return {
        "domain": capability["domain"],
        "route_key": capability["route_key"],
        "section": capability.get("section"),
    }


def deprecation_for_task(task_code: str) -> dict | None:
    owner = owner_for_task(task_code)
    if not owner:
        return None
    return {
        "deprecated": True,
        "message": "该生态入口仅用于兼容旧数据，请在归属业务模块中继续操作。",
        **owner,
    }


def _legacy_payload(task_code: str) -> dict:
    return {
        "legacy": True,
        "read_only": True,
        "owner": owner_for_task(task_code),
        "deprecation": deprecation_for_task(task_code),
    }


def config_to_dict(row: EcosystemIntegrationConfig) -> dict:
    return {
        "id": row.id,
        "task_code": row.task_code,
        "integration_type": row.integration_type,
        "config_name": row.config_name,
        "endpoint_url": row.endpoint_url,
        "auth_type": row.auth_type,
        "username": row.username,
        "has_secret": bool(row.secret_cipher),
        "extra_config": row.extra_config or {},
        "enabled": row.enabled,
        "remark": row.remark,
        "create_time": row.create_time,
        "update_time": row.update_time,
        **_legacy_payload(row.task_code),
    }


def event_to_dict(row: EcosystemIntegrationEvent) -> dict:
    return {
        "id": row.id,
        "task_code": row.task_code,
        "integration_type": row.integration_type,
        "event_type": row.event_type,
        "target_name": row.target_name,
        "status": row.status,
        "level": row.level,
        "title": row.title,
        "content": row.content,
        "payload_json": row.payload_json or {},
        "response_text": row.response_text,
        "error_message": row.error_message,
        "create_time": row.create_time,
        **_legacy_payload(row.task_code),
    }


def plan_to_dict(row: EcosystemIntegrationPlan) -> dict:
    return {
        "id": row.id,
        "task_code": row.task_code,
        "integration_type": row.integration_type,
        "plan_name": row.plan_name,
        "plan_type": row.plan_type,
        "status": row.status,
        "sections": row.sections_json or [],
        "warnings": row.warnings_json or [],
        "create_time": row.create_time,
        "update_time": row.update_time,
        **_legacy_payload(row.task_code),
    }


def list_tasks() -> list[dict]:
    return TASKS


def get_task(task_code: str) -> dict | None:
    return get_capability(task_code)


def overview(db: Session) -> dict:
    return {"items": summarize_capabilities(db), "legacy_migration": legacy_migration_status(db)}


def _migration_marker(db: Session) -> EcosystemIntegrationEvent | None:
    return db.query(EcosystemIntegrationEvent).filter(
        EcosystemIntegrationEvent.event_type == "migration",
        EcosystemIntegrationEvent.target_name == LEGACY_MIGRATION_VERSION,
    ).first()


def legacy_migration_status(db: Session) -> dict:
    marker = _migration_marker(db)
    return {
        "version": LEGACY_MIGRATION_VERSION,
        "applied": marker is not None,
        "applied_at": marker.create_time if marker else None,
        "summary": marker.payload_json if marker else None,
    }


def migrate_legacy_records(db: Session) -> dict:
    existing = _migration_marker(db)
    if existing:
        return {**legacy_migration_status(db), "already_applied": True, "marker_id": existing.id}

    task_codes = list(CAPABILITY_REGISTRY)
    config_count = db.query(EcosystemIntegrationConfig).filter(EcosystemIntegrationConfig.task_code.in_(task_codes)).count()
    event_count = db.query(EcosystemIntegrationEvent).filter(EcosystemIntegrationEvent.task_code.in_(task_codes)).count()
    plan_count = db.query(EcosystemIntegrationPlan).filter(EcosystemIntegrationPlan.task_code.in_(task_codes)).count()
    summary = {
        "mode": "read_only_legacy_history",
        "config_count": config_count,
        "event_count": event_count,
        "plan_count": plan_count,
        "record_count": config_count + event_count + plan_count,
        "owners": {task_code: owner_for_task(task_code) for task_code in task_codes},
        "deleted_count": 0,
    }
    marker = EcosystemIntegrationEvent(
        task_code="SYSTEM",
        integration_type="migration",
        event_type="migration",
        target_name=LEGACY_MIGRATION_VERSION,
        status="success",
        level="info",
        title="生态能力领域归位迁移标记",
        content="旧生态配置、事件和方案已保留为只读历史，未删除任何记录。",
        payload_json=summary,
    )
    db.add(marker)
    db.commit()
    db.refresh(marker)
    return {**legacy_migration_status(db), "already_applied": False, "marker_id": marker.id}


def list_configs(db: Session, task_code: str | None = None, integration_type: str | None = None) -> list[dict]:
    query = db.query(EcosystemIntegrationConfig)
    if task_code:
        query = query.filter(EcosystemIntegrationConfig.task_code == task_code)
    if integration_type:
        query = query.filter(EcosystemIntegrationConfig.integration_type == integration_type)
    rows = query.order_by(EcosystemIntegrationConfig.id.desc()).all()
    return [config_to_dict(row) for row in rows]


def get_config(db: Session, config_id: int) -> EcosystemIntegrationConfig | None:
    return db.query(EcosystemIntegrationConfig).filter(EcosystemIntegrationConfig.id == config_id).first()


def create_config(db: Session, data: dict) -> dict:
    row = EcosystemIntegrationConfig(
        task_code=data["task_code"],
        integration_type=data["integration_type"],
        config_name=data["config_name"],
        endpoint_url=data.get("endpoint_url"),
        auth_type=data.get("auth_type"),
        username=data.get("username"),
        secret_cipher=encrypt_password(data["secret"]) if data.get("secret") else None,
        extra_config=data.get("extra_config") or {},
        enabled=data.get("enabled", 1),
        remark=data.get("remark"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return config_to_dict(row)


def update_config(db: Session, row: EcosystemIntegrationConfig, data: dict) -> dict:
    for field in ["task_code", "integration_type", "config_name", "endpoint_url", "auth_type", "username", "extra_config", "enabled", "remark"]:
        if field in data:
            setattr(row, field, data[field])
    if data.get("secret"):
        row.secret_cipher = encrypt_password(data["secret"])
    db.commit()
    db.refresh(row)
    return config_to_dict(row)


def delete_config(db: Session, row: EcosystemIntegrationConfig) -> None:
    owner = owner_for_task(row.task_code)
    if owner:
        raise ValueError(f"旧生态配置为只读历史，请前往 {owner['route_key']} 管理当前配置")
    db.delete(row)
    db.commit()


def test_config(db: Session, row: EcosystemIntegrationConfig) -> dict:
    event = EcosystemIntegrationEvent(
        task_code=row.task_code,
        integration_type=row.integration_type,
        event_type="test",
        target_name=row.config_name,
        status="plan_only",
        level="info",
        title=f"{row.config_name} 测试连接",
        content="已生成测试记录。外部连接默认不在当前环境直接执行，请在客户网络中启用真实连接器后测试。",
        payload_json={"endpoint_url": row.endpoint_url, "auth_type": row.auth_type, "has_secret": bool(row.secret_cipher)},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {
        "status": "plan_only",
        "message": "已生成连接测试记录，当前不会向外部系统发送敏感请求。",
        "event": event_to_dict(event),
    }


def list_events(
    db: Session,
    task_code: str | None = None,
    integration_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    query = db.query(EcosystemIntegrationEvent)
    if task_code:
        query = query.filter(EcosystemIntegrationEvent.task_code == task_code)
    if integration_type:
        query = query.filter(EcosystemIntegrationEvent.integration_type == integration_type)
    if status:
        query = query.filter(EcosystemIntegrationEvent.status == status)
    rows = query.order_by(EcosystemIntegrationEvent.id.desc()).limit(limit).all()
    return [event_to_dict(row) for row in rows]


def list_plans(db: Session, task_code: str | None = None, limit: int = 100) -> list[dict]:
    query = db.query(EcosystemIntegrationPlan)
    if task_code:
        query = query.filter(EcosystemIntegrationPlan.task_code == task_code)
    rows = query.order_by(EcosystemIntegrationPlan.id.desc()).limit(limit).all()
    return [plan_to_dict(row) for row in rows]
