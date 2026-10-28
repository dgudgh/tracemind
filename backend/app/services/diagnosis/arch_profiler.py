"""架构画像：识别 单机/RAC/DataGuard/ASM 等，并产出根因优先级模板。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagArchProfile
from app.services.diagnosis import constants as C

logger = logging.getLogger("app.services.diagnosis.arch_profiler")


def _detect_oracle(instance: DBInstance) -> dict:
    """通过数据字典识别 Oracle 架构特征。任何异常都降级为单机。"""
    info: dict = {"arch_type": "单机", "tags": [], "details": {}}
    try:
        from app.collectors.oracle.oracle_connection import oracle_connection

        with oracle_connection(instance) as conn:
            cur = conn.cursor()
            # RAC：实例数
            try:
                cur.execute("SELECT COUNT(*) FROM gv$instance")
                inst_count = int(cur.fetchone()[0] or 1)
            except Exception:
                inst_count = 1
            info["details"]["instance_count"] = inst_count
            if inst_count > 1:
                info["arch_type"] = "RAC"
                info["tags"].append("RAC")

            # Data Guard：角色 + DG 配置
            try:
                cur.execute("SELECT database_role, open_mode FROM v$database")
                role, open_mode = cur.fetchone()
                info["details"]["database_role"] = role
                info["details"]["open_mode"] = open_mode
                if role and role.upper() != "PRIMARY":
                    info["tags"].append("DataGuard备库")
                    info["arch_type"] = "DataGuard" if info["arch_type"] == "单机" else info["arch_type"]
            except Exception:
                pass
            try:
                cur.execute("SELECT COUNT(*) FROM v$dataguard_config")
                if int(cur.fetchone()[0] or 0) > 1:
                    if "DataGuard" not in info["tags"] and "DataGuard备库" not in info["tags"]:
                        info["tags"].append("DataGuard")
            except Exception:
                pass

            # ASM
            try:
                cur.execute("SELECT name, state, total_mb, free_mb FROM v$asm_diskgroup")
                asm_rows = cur.fetchall() or []
                if len(asm_rows) > 0:
                    info["tags"].append("ASM")
                    info["details"]["asm_diskgroups"] = [
                        {
                            "name": row[0],
                            "state": row[1],
                            "total_mb": float(row[2] or 0),
                            "free_mb": float(row[3] or 0),
                            "used_pct": round((1 - (float(row[3] or 0) / max(1.0, float(row[2] or 0)))) * 100, 1),
                        }
                        for row in asm_rows
                    ]
            except Exception:
                pass
            cur.close()
    except Exception as e:
        logger.info("arch profile oracle detect fallback for %s: %s", instance.id, e)
        info["details"]["detect_error"] = str(e)
    return info


def _priority_template(arch_type: str, tags: list[str]) -> dict:
    is_rac = arch_type == "RAC" or "RAC" in tags
    priority = C.RAC_PRIORITY if is_rac else C.DEFAULT_PRIORITY
    return {
        "mode": "RAC" if is_rac else "default",
        "order": priority,
        "note": "网络 > Cache Fusion > 存储 > 内存 > CPU" if is_rac else "网络 > 存储 > 内存 > CPU",
    }


def profile_architecture(db: Session, instance: DBInstance, refresh: bool = True) -> DiagArchProfile:
    """识别实例架构并落 diag_arch_profile（默认刷新）。"""
    existing = db.query(DiagArchProfile).filter(DiagArchProfile.instance_id == instance.id).first()
    if existing and not refresh:
        return existing

    if instance.db_type == "oracle":
        info = _detect_oracle(instance)
    else:
        info = {"arch_type": "单机", "tags": [], "details": {"db_type": instance.db_type}}

    try:
        from app.services.diagnosis import hardware_profiler

        info["details"]["hardware_profile"] = hardware_profiler.collect_hardware_profile(instance, arch_info=info)
    except Exception as e:
        logger.info("hardware profile fallback for %s: %s", instance.id, e)
        info["details"]["hardware_profile"] = {"status": "unavailable", "message": str(e)}

    template = _priority_template(info["arch_type"], info["tags"])

    if existing:
        existing.arch_type = info["arch_type"]
        existing.arch_tags = info["tags"]
        existing.details = info["details"]
        existing.priority_template = template
        db.commit()
        db.refresh(existing)
        return existing

    row = DiagArchProfile(
        instance_id=instance.id,
        arch_type=info["arch_type"],
        arch_tags=info["tags"],
        details=info["details"],
        priority_template=template,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def to_dict(row: DiagArchProfile) -> dict:
    return {
        "instance_id": row.instance_id,
        "arch_type": row.arch_type,
        "arch_tags": row.arch_tags or [],
        "details": row.details or {},
        "priority_template": row.priority_template or {},
        "detected_at": row.detected_at.isoformat(sep=" ") if row.detected_at else None,
    }
