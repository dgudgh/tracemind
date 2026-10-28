from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from sqlalchemy.orm import Session, object_session

from app.core.secrets import resolve_secret, secret_source, store_secret
from app.models.connection_certificate import ConnectionCertificateAudit, ConnectionCertificateProfile
from app.models.db_instance import DBInstance
from app.schemas.connection_security import CertificateProfileCreate, CertificateProfileUpdate


def list_profiles(db: Session) -> list[dict]:
    return [serialize_profile(row) for row in db.query(ConnectionCertificateProfile).order_by(ConnectionCertificateProfile.name).all()]


def get_profile(db: Session, profile_id: int) -> ConnectionCertificateProfile | None:
    return db.get(ConnectionCertificateProfile, profile_id)


def create_profile(db: Session, data: CertificateProfileCreate, actor: str) -> ConnectionCertificateProfile:
    if db.query(ConnectionCertificateProfile).filter(ConnectionCertificateProfile.name == data.name).first():
        raise ValueError("证书配置名称已存在")
    payload = data.model_dump(exclude={
        "private_key",
        "private_key_secret_ref",
        "wallet_password",
        "wallet_password_secret_ref",
    })
    _validate_wallet_directory(data.profile_type, data.wallet_directory)
    payload.update(_certificate_metadata(data.client_certificate_pem or data.ca_certificate_pem))
    if data.private_key or data.private_key_secret_ref:
        payload["private_key_cipher"] = store_secret(
            plaintext=data.private_key,
            reference=data.private_key_secret_ref,
        )
    if data.wallet_password or data.wallet_password_secret_ref:
        payload["wallet_password_cipher"] = store_secret(
            plaintext=data.wallet_password,
            reference=data.wallet_password_secret_ref,
        )
    if data.client_certificate_pem and data.private_key:
        _validate_certificate_private_key(data.client_certificate_pem, data.private_key)
    row = ConnectionCertificateProfile(**payload)
    db.add(row)
    db.flush()
    _audit(db, row.id, "created", actor, "证书配置已创建")
    db.commit()
    db.refresh(row)
    return row


def update_profile(
    db: Session,
    row: ConnectionCertificateProfile,
    data: CertificateProfileUpdate,
    actor: str,
) -> ConnectionCertificateProfile:
    values = data.model_dump(exclude_unset=True)
    private_key = values.pop("private_key", None)
    private_key_ref = values.pop("private_key_secret_ref", None)
    wallet_password = values.pop("wallet_password", None)
    wallet_password_ref = values.pop("wallet_password_secret_ref", None)
    if "name" in values:
        duplicate = db.query(ConnectionCertificateProfile).filter(
            ConnectionCertificateProfile.name == values["name"],
            ConnectionCertificateProfile.id != row.id,
        ).first()
        if duplicate:
            raise ValueError("证书配置名称已存在")
    wallet_directory = values.get("wallet_directory", row.wallet_directory)
    _validate_wallet_directory(row.profile_type, wallet_directory)
    cert_pem = values.get("client_certificate_pem", row.client_certificate_pem) or values.get(
        "ca_certificate_pem", row.ca_certificate_pem
    )
    if "client_certificate_pem" in values or "ca_certificate_pem" in values:
        values.update(_certificate_metadata(cert_pem))
    if private_key or private_key_ref:
        row.private_key_cipher = store_secret(plaintext=private_key, reference=private_key_ref)
    if wallet_password or wallet_password_ref:
        row.wallet_password_cipher = store_secret(
            plaintext=wallet_password,
            reference=wallet_password_ref,
        )
    client_cert = values.get("client_certificate_pem", row.client_certificate_pem)
    if client_cert and private_key:
        _validate_certificate_private_key(client_cert, private_key)
    changed_fields = sorted(key for key, value in values.items() if getattr(row, key, None) != value)
    for key, value in values.items():
        setattr(row, key, value)
    secret_changed = bool(private_key or private_key_ref or wallet_password or wallet_password_ref)
    detail = "证书配置已更新"
    if changed_fields:
        detail += f"；字段：{', '.join(changed_fields)}"
    if secret_changed:
        detail += "；秘密材料已轮换"
    _audit(db, row.id, "updated", actor, detail)
    db.commit()
    db.refresh(row)
    return row


def delete_profile(db: Session, row: ConnectionCertificateProfile, actor: str) -> None:
    references = db.query(DBInstance).filter(DBInstance.certificate_profile_id == row.id).count()
    if references:
        raise ValueError(f"仍有 {references} 个数据库连接使用该证书配置，不能删除")
    profile_id = row.id
    _audit(db, profile_id, "deleted", actor, "证书配置已删除，秘密材料未写入审计")
    db.delete(row)
    db.commit()


def test_profile(row: ConnectionCertificateProfile, db: Session, actor: str) -> dict:
    try:
        if row.profile_type == "pem":
            if not row.ca_certificate_pem and not row.client_certificate_pem:
                raise ValueError("证书内容为空")
            _certificate_metadata(row.client_certificate_pem or row.ca_certificate_pem)
            if row.client_certificate_pem:
                if not row.private_key_cipher:
                    raise ValueError("客户端证书缺少私钥")
                _validate_certificate_private_key(
                    row.client_certificate_pem,
                    resolve_secret(row.private_key_cipher),
                )
        else:
            _validate_wallet_directory(row.profile_type, row.wallet_directory)
            if not Path(row.wallet_directory or "").is_dir():
                raise ValueError("Oracle Wallet 目录在当前服务器上不存在")
            if row.wallet_password_cipher:
                resolve_secret(row.wallet_password_cipher)
        if row.lifecycle_status == "expired":
            raise ValueError("证书已经过期")
        result = {
            "ok": True,
            "message": "证书配置校验通过",
            "lifecycle_status": row.lifecycle_status,
            "not_after": row.not_after.isoformat() if row.not_after else None,
        }
        _audit(db, row.id, "tested", actor, result["message"])
        db.commit()
        return result
    except Exception as exc:
        _audit(db, row.id, "tested", actor, f"校验失败：{str(exc)[:500]}", outcome="failed")
        db.commit()
        raise


def audits(db: Session, profile_id: int, limit: int = 100):
    return (
        db.query(ConnectionCertificateAudit)
        .filter(ConnectionCertificateAudit.profile_id == profile_id)
        .order_by(ConnectionCertificateAudit.id.desc())
        .limit(limit)
        .all()
    )


def serialize_profile(row: ConnectionCertificateProfile) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "profile_type": row.profile_type,
        "fingerprint_sha256": row.fingerprint_sha256,
        "subject": row.subject,
        "issuer": row.issuer,
        "serial_number": row.serial_number,
        "not_before": row.not_before,
        "not_after": row.not_after,
        "enabled": row.enabled,
        "remark": row.remark,
        "has_ca_certificate": bool(row.ca_certificate_pem),
        "has_client_certificate": bool(row.client_certificate_pem),
        "has_private_key": bool(row.private_key_cipher),
        "private_key_source": secret_source(row.private_key_cipher),
        "has_wallet_password": bool(row.wallet_password_cipher),
        "wallet_password_source": secret_source(row.wallet_password_cipher),
        "lifecycle_status": row.lifecycle_status,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


@contextmanager
def database_tls_material(instance: DBInstance):
    profile = _instance_certificate_profile(instance)
    if profile is None:
        yield {}
        return
    _ensure_profile_usable(profile)
    if profile.profile_type != "pem":
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix="tracemind-tls-") as directory:
        root = Path(directory)
        material: dict[str, object] = {"verify": bool(instance.tls_verify)}
        if profile.ca_certificate_pem:
            material["ca"] = _write_private_file(root / "ca.pem", profile.ca_certificate_pem)
        if profile.client_certificate_pem:
            material["cert"] = _write_private_file(root / "client.pem", profile.client_certificate_pem)
        if profile.private_key_cipher:
            private_key = resolve_secret(profile.private_key_cipher)
            if profile.client_certificate_pem:
                _validate_certificate_private_key(profile.client_certificate_pem, private_key)
            material["key"] = _write_private_file(root / "client.key", private_key)
        if instance.tls_server_name:
            material["server_name"] = instance.tls_server_name
        yield material


def oracle_wallet_settings(instance: DBInstance) -> dict:
    profile = _instance_certificate_profile(instance)
    if profile is None or profile.profile_type != "oracle_wallet":
        return {}
    _ensure_profile_usable(profile)
    path = Path(profile.wallet_directory or "")
    if not path.is_dir():
        raise ValueError("Oracle Wallet 目录在当前服务器上不存在")
    result = {"wallet_location": str(path)}
    if profile.wallet_password_cipher:
        result["wallet_password"] = resolve_secret(profile.wallet_password_cipher)
    return result


def minimum_privilege_catalog() -> list[dict]:
    return [
        {
            "db_type": "oracle",
            "account_name_placeholder": "TRACEMIND_MONITOR",
            "capabilities": ["连接", "数据字典", "动态性能视图", "AWR 只读", "巡检"],
            "create_sql": [
                "CREATE USER TRACEMIND_MONITOR IDENTIFIED BY <PASSWORD> ACCOUNT UNLOCK",
                "GRANT CREATE SESSION TO TRACEMIND_MONITOR",
                "GRANT SELECT_CATALOG_ROLE TO TRACEMIND_MONITOR",
                "GRANT SELECT ON SYS.DBA_HIST_SNAPSHOT TO TRACEMIND_MONITOR",
                "GRANT SELECT ON SYS.DBA_HIST_SQLSTAT TO TRACEMIND_MONITOR",
            ],
            "revoke_sql": ["DROP USER TRACEMIND_MONITOR CASCADE"],
            "notes": ["生产环境可按模板实际检查项继续收窄对象授权", "不得授予 SYSDBA 作为日常巡检账号"],
        },
        {
            "db_type": "mysql",
            "account_name_placeholder": "tracemind_monitor",
            "capabilities": ["状态变量", "复制状态", "performance_schema", "sys 视图", "巡检"],
            "create_sql": [
                "CREATE USER 'tracemind_monitor'@'<SOURCE_HOST>' IDENTIFIED BY '<PASSWORD>'",
                "GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'tracemind_monitor'@'<SOURCE_HOST>'",
                "GRANT SELECT ON performance_schema.* TO 'tracemind_monitor'@'<SOURCE_HOST>'",
                "GRANT SELECT ON sys.* TO 'tracemind_monitor'@'<SOURCE_HOST>'",
            ],
            "revoke_sql": ["DROP USER 'tracemind_monitor'@'<SOURCE_HOST>'"],
            "notes": ["限制来源主机，不使用 %", "不授予写权限或 SUPER"],
        },
        {
            "db_type": "postgresql",
            "account_name_placeholder": "tracemind_monitor",
            "capabilities": ["活动会话", "复制状态", "统计视图", "配置读取", "巡检"],
            "create_sql": [
                "CREATE ROLE tracemind_monitor LOGIN PASSWORD '<PASSWORD>'",
                "GRANT pg_monitor TO tracemind_monitor",
                "GRANT CONNECT ON DATABASE <DATABASE> TO tracemind_monitor",
            ],
            "revoke_sql": ["DROP ROLE tracemind_monitor"],
            "notes": ["pg_monitor 不包含业务表数据读取权限", "在 pg_hba.conf 中限制来源地址并启用 SCRAM"],
        },
        {
            "db_type": "sqlserver",
            "account_name_placeholder": "tracemind_monitor",
            "capabilities": ["服务器状态", "数据库状态", "Always On", "执行计划元数据", "巡检"],
            "create_sql": [
                "CREATE LOGIN tracemind_monitor WITH PASSWORD = '<PASSWORD>'",
                "GRANT VIEW SERVER STATE TO tracemind_monitor",
                "GRANT VIEW ANY DEFINITION TO tracemind_monitor",
                "GRANT CONNECT ANY DATABASE TO tracemind_monitor",
            ],
            "revoke_sql": ["DROP LOGIN tracemind_monitor"],
            "notes": ["SQL Server 2022 可按需改用 VIEW SERVER PERFORMANCE STATE", "不授予 sysadmin"],
        },
    ]


def compatibility_matrix() -> list[dict]:
    return [
        {"db_type": "oracle", "versions": "11.2-23ai", "driver": "python-oracledb", "tls": "Oracle Wallet / TCPS", "architecture": ["single", "rac", "adg"]},
        {"db_type": "mysql", "versions": "5.7-8.4", "driver": "PyMySQL", "tls": "CA 与双向 PEM", "architecture": ["single", "primary_replica", "mgr"]},
        {"db_type": "postgresql", "versions": "10-17", "driver": "psycopg3 / psycopg2", "tls": "sslmode、CA 与双向 PEM", "architecture": ["single", "streaming_replication"]},
        {"db_type": "sqlserver", "versions": "2016-2022", "driver": "pymssql", "tls": "系统信任链与强制加密", "architecture": ["single", "alwayson"]},
    ]


def _certificate_metadata(pem: str | None) -> dict:
    if not pem:
        return {
            "fingerprint_sha256": None,
            "subject": None,
            "issuer": None,
            "serial_number": None,
            "not_before": None,
            "not_after": None,
        }
    try:
        certificate = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except ValueError as exc:
        raise ValueError("证书 PEM 内容无效") from exc
    return {
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "subject": certificate.subject.rfc4514_string()[:500],
        "issuer": certificate.issuer.rfc4514_string()[:500],
        "serial_number": format(certificate.serial_number, "x"),
        "not_before": certificate.not_valid_before_utc.replace(tzinfo=None),
        "not_after": certificate.not_valid_after_utc.replace(tzinfo=None),
    }


def _validate_certificate_private_key(certificate_pem: str, private_key_pem: str) -> None:
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
        private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        cert_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("客户端证书或私钥 PEM 内容无效") from exc
    if cert_public != key_public:
        raise ValueError("客户端证书与私钥不匹配")


def _validate_wallet_directory(profile_type: str, value: str | None) -> None:
    if profile_type != "oracle_wallet":
        return
    path = Path(value or "")
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Oracle Wallet 必须使用不含上级跳转的服务器绝对目录")


def _instance_certificate_profile(instance: DBInstance) -> ConnectionCertificateProfile | None:
    if not instance.certificate_profile_id:
        return None
    profile = getattr(instance, "certificate_profile", None)
    if profile is not None:
        return profile
    session = object_session(instance)
    return session.get(ConnectionCertificateProfile, instance.certificate_profile_id) if session else None


def _ensure_profile_usable(profile: ConnectionCertificateProfile) -> None:
    if not profile.enabled:
        raise ValueError("数据库连接引用的证书配置已停用")
    if profile.lifecycle_status == "expired":
        raise ValueError("数据库连接引用的证书已经过期")


def _write_private_file(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _audit(
    db: Session,
    profile_id: int,
    action: str,
    actor: str,
    detail: str,
    outcome: str = "success",
) -> None:
    db.add(ConnectionCertificateAudit(
        profile_id=profile_id,
        action=action,
        actor=(actor or "system")[:100],
        outcome=outcome,
        detail=detail[:1000],
    ))
