import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collectors.dm.dm_connection import test_dm_connection
from app.collectors.kingbasees.kingbasees_connection import test_kingbasees_connection
from app.collectors.mysql.mysql_connection import test_mysql_connection
from app.collectors.oracle.oracle_connection import test_oracle_connection
from app.collectors.postgresql.postgresql_connection import test_postgresql_connection
from app.collectors.sqlserver.sqlserver_connection import test_sqlserver_connection
from app.cmdb.normalized_service import deactivate_native_instance, record_native_connection_test
from app.core.secrets import rewrap_secret, secret_source, store_secret
from app.services.access_control import sanitize_audit_details
from app.models.db_instance import DBInstance, DBInstanceCredentialAudit
from app.schemas.db_instance import (
    DBInstanceConnectionTest,
    DBInstanceCreate,
    DBInstanceCredentialRotate,
    DBInstanceUpdate,
)

logger = logging.getLogger("app.services.instances")


def get_list(
    db: Session,
    page: int = 1,
    page_size: int = 10,
    db_type: str | None = None,
    keyword: str | None = None,
    allowed_ids: set[int] | frozenset[int] | None = None,
):
    query = db.query(DBInstance)
    if allowed_ids is not None:
        query = query.filter(DBInstance.id.in_(allowed_ids))
    if db_type:
        query = query.filter(DBInstance.db_type == db_type)
    if keyword:
        query = query.filter(DBInstance.instance_name.like(f"%{keyword}%"))
    total = query.count()
    items = (
        query.order_by(DBInstance.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def get_by_id(db: Session, instance_id: int):
    return db.query(DBInstance).filter(DBInstance.id == instance_id).first()


def _reserve_sqlite_identity(db: Session, *, retired_id: int | None = None) -> int | None:
    if db.get_bind().dialect.name != "sqlite":
        return None
    from app.core.transaction_lock import lock_transaction

    counter = lock_transaction(db, "db_instance_identity")
    largest = db.query(func.max(DBInstance.id)).scalar() or 0
    audited = db.query(func.max(DBInstanceCredentialAudit.instance_id)).scalar() or 0
    current = max(int(counter.value or 0), largest, audited, retired_id or 0)
    counter.value = str(current if retired_id is not None else current + 1)
    db.flush()
    return int(counter.value)


def create(db: Session, data: DBInstanceCreate, actor: str = "system"):
    payload = data.model_dump(exclude={
        "password",
        "password_secret_ref",
        "ssh_password",
        "ssh_password_secret_ref",
    })
    if payload.get("db_type") != "oracle":
        payload["account_type"] = "system" if payload.get("account_type") == "sysdba" else payload.get("account_type")
        payload["oracle_connect_type"] = None
        payload["service_name"] = None
        payload["sid"] = None
    certificate_profile = _certificate_profile_for_write(db, payload.get("certificate_profile_id"))
    now = datetime.now()
    payload["password_cipher"] = store_secret(
        plaintext=data.password,
        reference=data.password_secret_ref,
    )
    payload["password_rotated_at"] = now
    if data.ssh_password or data.ssh_password_secret_ref:
        payload["ssh_password_cipher"] = store_secret(
            plaintext=data.ssh_password,
            reference=data.ssh_password_secret_ref,
        )
        payload["ssh_password_rotated_at"] = now
    obj = DBInstance(**payload)
    identity = _reserve_sqlite_identity(db)
    if identity is not None:
        obj.id = identity
    if certificate_profile is not None:
        obj.certificate_profile = certificate_profile
    db.add(obj)
    db.flush()
    _credential_audit(
        db,
        obj,
        kind="database",
        action="created",
        actor=actor,
        source_after=secret_source(obj.password_cipher),
        reason="数据库连接凭据已创建",
    )
    if obj.ssh_password_cipher:
        _credential_audit(
            db,
            obj,
            kind="ssh",
            action="created",
            actor=actor,
            source_after=secret_source(obj.ssh_password_cipher),
            reason="SSH 连接凭据已创建",
        )
    db.commit()
    db.refresh(obj)
    logger.info("创建实例 | id=%s | name=%s | type=%s | host=%s:%s", obj.id, obj.instance_name, obj.db_type, obj.host, obj.port)
    return obj


def update(db: Session, obj: DBInstance, data: DBInstanceUpdate, actor: str = "system"):
    update_data = data.model_dump(exclude_unset=True)
    password = update_data.pop("password", None)
    password_secret_ref = update_data.pop("password_secret_ref", None)
    ssh_password = update_data.pop("ssh_password", None)
    ssh_password_secret_ref = update_data.pop("ssh_password_secret_ref", None)
    changed_fields = sorted(
        key for key, value in update_data.items() if getattr(obj, key, None) != value
    )
    certificate_profile = None
    if "certificate_profile_id" in update_data:
        certificate_profile = _certificate_profile_for_write(db, update_data.get("certificate_profile_id"))
    target_db_type = update_data.get("db_type", obj.db_type)
    if target_db_type != "oracle":
        if update_data.get("account_type", obj.account_type) == "sysdba":
            update_data["account_type"] = "system"
        update_data["oracle_connect_type"] = None
        update_data["service_name"] = None
        update_data["sid"] = None
    for key, value in update_data.items():
        setattr(obj, key, value)
    if "certificate_profile_id" in update_data:
        obj.certificate_profile = certificate_profile
    if password or password_secret_ref:
        source_before = secret_source(obj.password_cipher)
        obj.password_cipher = store_secret(
            plaintext=password,
            reference=password_secret_ref,
        )
        obj.password_rotated_at = datetime.now()
        _credential_audit(
            db,
            obj,
            kind="database",
            action="rotated",
            actor=actor,
            source_before=source_before,
            source_after=secret_source(obj.password_cipher),
            reason="数据库连接凭据已更新",
        )
    if ssh_password or ssh_password_secret_ref:
        source_before = secret_source(obj.ssh_password_cipher)
        obj.ssh_password_cipher = store_secret(
            plaintext=ssh_password,
            reference=ssh_password_secret_ref,
        )
        obj.ssh_password_rotated_at = datetime.now()
        _credential_audit(
            db,
            obj,
            kind="ssh",
            action="rotated",
            actor=actor,
            source_before=source_before,
            source_after=secret_source(obj.ssh_password_cipher),
            reason="SSH 连接凭据已更新",
        )
    if changed_fields:
        _credential_audit(
            db,
            obj,
            kind="configuration",
            action="updated",
            actor=actor,
            reason=f"连接配置字段已更新：{', '.join(changed_fields)}",
        )
    db.commit()
    db.refresh(obj)
    logger.info("更新实例 | id=%s | name=%s", obj.id, obj.instance_name)
    return obj


def delete(db: Session, obj: DBInstance, operator: str = "admin"):
    logger.info("删除实例 | id=%s | name=%s", obj.id, obj.instance_name)
    _reserve_sqlite_identity(db, retired_id=obj.id)
    _credential_audit(
        db,
        obj,
        kind="configuration",
        action="deleted",
        actor=operator,
        source_before=secret_source(obj.password_cipher),
        reason="数据库连接配置已删除，凭据密文未写入审计",
    )
    deactivate_native_instance(db, obj.id, operator=operator)
    db.delete(obj)
    db.commit()


def test_connection(instance: DBInstance, *, force_live: bool = False) -> dict:
    logger.info("测试连接 | id=%s | name=%s | type=%s | host=%s:%s", instance.id, instance.instance_name, instance.db_type, instance.host, instance.port)
    try:
        if instance.db_type == "oracle":
            result = test_oracle_connection(instance, force_live=force_live)
        elif instance.db_type == "mysql":
            result = test_mysql_connection(instance)
        elif instance.db_type == "postgresql":
            result = test_postgresql_connection(instance)
        elif instance.db_type == "sqlserver":
            result = test_sqlserver_connection(instance)
        elif instance.db_type == "dm":
            result = test_dm_connection(instance)
        elif instance.db_type == "kingbasees":
            result = test_kingbasees_connection(instance)
        else:
            raise ValueError(f"Unsupported db_type: {instance.db_type}")
        logger.info("连接测试成功 | id=%s | type=%s", instance.id, instance.db_type)
        return result
    except Exception as exc:
        logger.error("连接测试失败 | id=%s | type=%s | error=%s", instance.id, instance.db_type, exc)
        raise


def test_connection_and_record(db: Session, instance: DBInstance, actor: str = "system") -> dict:
    try:
        result = test_connection(instance, force_live=True)
    except Exception as exc:
        try:
            record_native_connection_test(
                db,
                instance.id,
                ok=False,
                error=str(exc),
            )
        except Exception:
            db.rollback()
            logger.warning(
                "连接失败状态写入资产中心失败 | id=%s",
                instance.id,
                exc_info=True,
            )
        _safe_test_audit(db, instance, actor=actor, outcome="failed", reason=str(exc))
        raise

    try:
        record_native_connection_test(
            db,
            instance.id,
            ok=True,
            detail=result,
        )
    except Exception:
        db.rollback()
        logger.warning(
            "连接成功状态写入资产中心失败 | id=%s",
            instance.id,
            exc_info=True,
        )
    _safe_test_audit(db, instance, actor=actor, outcome="success", reason="连接测试通过")
    return result


def test_connection_configuration(db: Session, data: DBInstanceConnectionTest) -> dict:
    existing = get_by_id(db, data.instance_id) if data.instance_id else None
    if data.instance_id and not existing:
        raise ValueError("数据库连接配置不存在")
    if not data.password and not data.password_secret_ref and not existing:
        raise ValueError("请先填写数据库密码或外部秘密引用")

    payload = data.model_dump(exclude={
        "instance_id",
        "password",
        "password_secret_ref",
        "ssh_password",
        "ssh_password_secret_ref",
    })
    if payload.get("db_type") != "oracle":
        payload["account_type"] = "system" if payload.get("account_type") == "sysdba" else payload.get("account_type")
        payload["oracle_connect_type"] = None
        payload["service_name"] = None
        payload["sid"] = None
    payload["password_cipher"] = (
        store_secret(plaintext=data.password, reference=data.password_secret_ref)
        if data.password or data.password_secret_ref
        else existing.password_cipher
    )
    candidate = DBInstance(**payload)
    candidate.id = existing.id if existing else 0
    if candidate.certificate_profile_id:
        candidate.certificate_profile = _certificate_profile_for_write(db, candidate.certificate_profile_id)
    return test_connection(candidate, force_live=True)


def rotate_credential(
    db: Session,
    obj: DBInstance,
    data: DBInstanceCredentialRotate,
    actor: str = "system",
) -> DBInstance:
    is_ssh = data.credential_kind == "ssh"
    field = "ssh_password_cipher" if is_ssh else "password_cipher"
    rotated_field = "ssh_password_rotated_at" if is_ssh else "password_rotated_at"
    current = getattr(obj, field)
    if not current and not data.password and not data.secret_ref:
        raise ValueError("当前凭据不存在，请提供新密码或外部秘密引用")
    source_before = secret_source(current)
    stored = (
        store_secret(plaintext=data.password, reference=data.secret_ref)
        if data.password or data.secret_ref
        else rewrap_secret(current)
    )
    setattr(obj, field, stored)
    setattr(obj, rotated_field, datetime.now())
    _credential_audit(
        db,
        obj,
        kind=data.credential_kind,
        action="rotated" if data.password or data.secret_ref else "rewrapped",
        actor=actor,
        source_before=source_before,
        source_after=secret_source(stored),
        reason=data.reason,
    )
    db.commit()
    db.refresh(obj)
    return obj


def credential_audits(db: Session, instance_id: int, limit: int = 100):
    return (
        db.query(DBInstanceCredentialAudit)
        .filter(DBInstanceCredentialAudit.instance_id == instance_id)
        .order_by(DBInstanceCredentialAudit.id.desc())
        .limit(limit)
        .all()
    )


def _credential_audit(
    db: Session,
    obj: DBInstance,
    *,
    kind: str,
    action: str,
    actor: str,
    source_before: str | None = None,
    source_after: str | None = None,
    outcome: str = "success",
    reason: str | None = None,
) -> DBInstanceCredentialAudit:
    row = DBInstanceCredentialAudit(
        instance_id=obj.id,
        credential_kind=kind,
        action=action,
        actor=(actor or "system")[:100],
        source_before=source_before,
        source_after=source_after,
        outcome=outcome,
        reason=(reason or "")[:1000] or None,
    )
    db.add(row)
    return row


def _safe_test_audit(
    db: Session,
    instance: DBInstance,
    *,
    actor: str,
    outcome: str,
    reason: str,
) -> None:
    try:
        _credential_audit(
            db,
            instance,
            kind="database",
            action="connection_tested",
            actor=actor,
            source_before=secret_source(instance.password_cipher),
            source_after=secret_source(instance.password_cipher),
            outcome=outcome,
            reason=str(sanitize_audit_details(reason)),
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("连接测试审计写入失败 | id=%s", instance.id, exc_info=True)


def _certificate_profile_for_write(db: Session, profile_id: int | None):
    if not profile_id:
        return None
    from app.models.connection_certificate import ConnectionCertificateProfile

    profile = db.get(ConnectionCertificateProfile, profile_id)
    if not profile:
        raise ValueError("选择的证书配置不存在")
    if not profile.enabled:
        raise ValueError("选择的证书配置已停用")
    if profile.lifecycle_status == "expired":
        raise ValueError("选择的证书已经过期")
    return profile
