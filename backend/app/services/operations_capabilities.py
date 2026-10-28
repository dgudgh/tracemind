"""Truthful capability catalog for the four supported database engines."""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from sqlalchemy.orm import Session

from app.cmdb.models import AssetInstance
from app.models.db_instance import DBInstance


ENGINE_ORDER = ("oracle", "mysql", "postgresql", "sqlserver")
ENGINE_LABELS = {
    "oracle": "Oracle",
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "sqlserver": "SQL Server",
}

MODE_LABELS = {
    "read_only": "只读查询",
    "plan_only": "生成方案",
    "approval_required": "审批后执行",
    "unsupported": "不支持",
}

MATURITY_LABELS = {
    "ready": "已实现",
    "partial": "部分实现",
    "planned": "待建设",
    "blocked": "明确阻断",
}

#: Maturity that must never be hand-written because another module already owns the truth.
#: Backup/restore maturity is derived from :func:`app.services.backup_center.get_operation_capability`
#: so the catalog cannot drift away from what the backup centre will actually execute.
DERIVED_MATURITY: dict[str, str] = {
    "protection.backup": "backup",
    "protection.restore": "restore",
}


def _modes(value: str, *, unsupported: tuple[str, ...] = ()) -> dict[str, str]:
    return {engine: ("unsupported" if engine in unsupported else value) for engine in ENGINE_ORDER}


def _maturity(default: str = "ready", **overrides: str) -> dict[str, str]:
    return {engine: overrides.get(engine, default) for engine in ENGINE_ORDER}


def protection_maturity(category: str) -> dict[str, str]:
    """Derive per-engine backup/restore maturity from the real execution capability.

    Restating these values in the catalog is what produced the S1 defect: the matrix claimed
    "ready" for every engine while the backup centre returns ``executable=False`` for SQL Server
    and refuses MySQL/PostgreSQL restores outright. A derived value cannot drift; a copied one
    always does.
    """
    from app.services import backup_center

    # 开源版不含备份恢复实现：成熟度统一记为 planned，不臆造 ready。
    return _maturity("planned")

    maturity: dict[str, str] = {}
    for engine in ENGINE_ORDER:
        capabilities = [
            backup_center.get_operation_capability(engine, plan_type)
            for plan_type in backup_center.OPERATION_LABELS.get(engine, {})
        ]
        matched = [item for item in capabilities if item["category"] == category]
        if not matched:
            maturity[engine] = "planned"
        elif any(item["server_execution"] for item in matched):
            maturity[engine] = "ready"
        elif all(item["unavailable_reason"] == backup_center.LOGICAL_RESTORE_BLOCK_REASON
                 for item in matched):
            maturity[engine] = "blocked"
        else:
            maturity[engine] = "partial"
    return maturity


CAPABILITY_CATALOG: tuple[dict, ...] = (
    {
        "key": "runtime.overview",
        "domain": "运行与会话",
        "name": "实例运行概览",
        "description": "版本、启动状态、连接、资源限制和核心运行参数。",
        "risk": "low",
        "route_key": "ops.overview",
        "modes": _modes("read_only"),
        "maturity": _maturity(),
    },
    {
        "key": "runtime.sessions",
        "domain": "运行与会话",
        "name": "会话、事务与阻塞链",
        "description": "活动会话、长事务、锁等待、阻塞链和当前 SQL。",
        "risk": "low",
        "route_key": "ops.session",
        "modes": _modes("read_only"),
        "maturity": _maturity(),
    },
    {
        "key": "runtime.terminate_session",
        "domain": "运行与会话",
        "name": "终止会话与解除阻塞",
        "description": "真实会话终止尚未授权或完成实机验收，保持不支持，不得以审批代替放行。",
        "risk": "high",
        "route_key": "ops.session",
        "modes": _modes("unsupported"),
        "maturity": _maturity("blocked"),
    },
    {
        "key": "configuration.parameters.read",
        "domain": "参数与配置",
        "name": "参数与配置漂移",
        "description": "查看当前值、动态属性、作用域、期望值和重启要求。",
        "risk": "low",
        "route_key": "ops.overview",
        "modes": _modes("read_only"),
        "maturity": _maturity("partial", oracle="ready", mysql="ready"),
    },
    {
        "key": "configuration.parameters.change",
        "domain": "参数与配置",
        "name": "参数变更方案",
        "description": "生成变更、验证和回滚脚本；当前运维页面不直接修改生产参数。",
        "risk": "high",
        "route_key": "ops.overview",
        "modes": _modes("plan_only"),
        "maturity": _maturity("partial", oracle="ready", mysql="ready"),
    },
    {
        "key": "storage.capacity",
        "domain": "空间与对象",
        "name": "空间容量与增长",
        "description": "数据库、表空间、文件、日志、对象容量和增长风险。",
        "risk": "low",
        "route_key": "ops.space",
        "modes": _modes("read_only"),
        "maturity": _maturity(),
    },
    {
        "key": "storage.expand",
        "domain": "空间与对象",
        "name": "容量扩展方案",
        "description": "生成扩容前检查、执行脚本、容量验证和回滚边界。",
        "risk": "high",
        "route_key": "ops.space",
        "modes": _modes("plan_only"),
        "maturity": _maturity("partial", oracle="ready", mysql="ready"),
    },
    {
        "key": "objects.statistics",
        "domain": "空间与对象",
        "name": "对象与统计信息",
        "description": "表、索引、分区、无效对象、碎片和统计信息状态。",
        "risk": "low",
        "route_key": "ops.object",
        "modes": _modes("read_only"),
        "maturity": _maturity(),
    },
    {
        "key": "objects.maintenance",
        "domain": "空间与对象",
        "name": "对象维护方案",
        "description": "生成统计信息刷新、索引维护、VACUUM 或重建建议和复核步骤。",
        "risk": "medium",
        "route_key": "ops.object",
        "modes": _modes("plan_only"),
        "maturity": _maturity("partial", oracle="ready", mysql="ready"),
    },
    {
        "key": "logs.runtime",
        "domain": "日志与恢复",
        "name": "数据库日志与归档",
        "description": "Redo、Archive、WAL、Error Log、事务日志和保留状态。",
        "risk": "low",
        "route_key": "ops.log",
        "modes": _modes("read_only", unsupported=("mysql",)),
        "maturity": _maturity("partial", oracle="ready", sqlserver="ready"),
    },
    {
        "key": "ha.health",
        "domain": "高可用",
        "name": "高可用健康诊断",
        "description": "角色、复制状态、延迟、日志缺口、仲裁和脑裂风险。",
        "risk": "low",
        "route_key": "ops.ha",
        "modes": _modes("read_only"),
        "maturity": _maturity(),
    },
    {
        "key": "ha.switchover",
        "domain": "高可用",
        "name": "计划切换与故障切换",
        "description": "当前提供实时前置检查和可审计方案，不从运维页面直接切换生产集群。",
        "risk": "critical",
        "route_key": "ops.ha",
        "modes": _modes("plan_only"),
        "maturity": _maturity("partial"),
    },
    {
        "key": "protection.backup",
        "domain": "数据保护",
        "name": "备份执行",
        "description": "通过备份策略、任务、执行器和审计链发起真实备份。成熟度按各引擎真实执行能力派生，不由本目录声明。",
        "risk": "medium",
        "route_key": "backup.plan",
        "modes": _modes("approval_required"),
        "maturity": None,  # derived: see DERIVED_MATURITY / protection_maturity
    },
    {
        "key": "protection.restore",
        "domain": "数据保护",
        "name": "恢复与演练",
        "description": "恢复任务、隔离演练、结果验证、RPO/RTO 和报告。成熟度按各引擎真实执行能力派生，不由本目录声明。",
        "risk": "critical",
        "route_key": "backup.restore",
        "modes": _modes("approval_required"),
        "maturity": None,  # derived: see DERIVED_MATURITY / protection_maturity
    },
    {
        "key": "governance.archive",
        "domain": "数据治理",
        "name": "归档与生命周期治理",
        "description": "候选分析、归档方案、校验、清理和恢复入口。",
        "risk": "high",
        "route_key": "ops.governance",
        "modes": _modes("plan_only"),
        "maturity": _maturity("partial"),
    },
)


def _asset_attributes(asset: AssetInstance | None) -> dict:
    return asset.attributes if asset and isinstance(asset.attributes, dict) else {}


def _instance_runtime(instance: DBInstance, asset: AssetInstance | None) -> dict:
    attributes = _asset_attributes(asset)
    connection_status = str(attributes.get("connection_test_status") or "unknown").lower()
    if connection_status not in {"success", "failed", "unknown"}:
        connection_status = "unknown"
    return {
        "id": instance.id,
        "name": instance.instance_name,
        "engine": instance.db_type,
        "enabled": bool(instance.enabled),
        "credential_ready": bool(instance.password_cipher),
        "credential_source": instance.credential_source,
        "credential_requires_rotation": bool(instance.credential_requires_rotation),
        "account_type": instance.account_type or "system",
        "connection_status": connection_status,
        "last_connection_test_at": attributes.get("last_connection_test_at"),
        "asset_status": asset.status if asset else "unmanaged",
    }


def _availability(mode: str, maturity: str, runtime: dict | None, can_execute: bool) -> tuple[str, str]:
    if mode == "unsupported" or maturity == "planned":
        return "unsupported", "当前引擎尚未提供该能力"
    if maturity == "blocked":
        # Engine-level refusal, independent of the selected instance: no credential, permission
        # or connection state can make it runnable, so it must never be reported as available.
        return "blocked", "当前引擎的该能力已被平台明确阻断，不提供执行入口"
    if runtime is None:
        return "not_configured", "当前引擎没有可用实例"
    if not runtime["enabled"]:
        return "blocked", "实例已停用"
    if mode == "approval_required" and not can_execute:
        return "permission_denied", "当前账号缺少运维执行权限"
    if mode == "plan_only":
        if runtime["connection_status"] == "failed":
            return "plan_available", "实时采集失败，仍可生成方案但必须先修复连接"
        return "plan_available", "可生成方案，生产执行需进入审批流程"
    if not runtime["credential_ready"]:
        return "blocked", "实例凭据未配置"
    if runtime["connection_status"] == "failed":
        return "blocked", "最近一次连接测试失败"
    if runtime["connection_status"] == "unknown":
        return "unverified", "尚无有效连接测试，请先执行测试"
    if runtime["credential_requires_rotation"]:
        return "degraded", "能力可用，但本地凭据已进入轮换提醒"
    if maturity == "partial":
        return "partial", "当前仅覆盖已实现的采集或方案范围"
    return "available", "能力可用"


def build_capability_matrix(
    db: Session,
    *,
    instances: Iterable[DBInstance] | None = None,
    engine: str | None = None,
    selected_instance_id: int | None = None,
    can_execute: bool = True,
) -> dict:
    if engine and engine not in ENGINE_ORDER:
        raise ValueError(f"Unsupported engine: {engine}")

    rows = list(instances if instances is not None else db.query(DBInstance).order_by(DBInstance.id).all())
    rows = [item for item in rows if item.db_type in ENGINE_ORDER]
    instance_ids = [item.id for item in rows]
    assets = {
        item.native_instance_id: item
        for item in db.query(AssetInstance).filter(AssetInstance.native_instance_id.in_(instance_ids or [-1])).all()
        if item.native_instance_id is not None
    }
    runtimes = [_instance_runtime(item, assets.get(item.id)) for item in rows]
    selected = next((item for item in runtimes if item["id"] == selected_instance_id), None)
    if selected_instance_id is not None and selected is None:
        raise LookupError("Instance is outside the current data scope or does not exist")
    if engine and selected and selected["engine"] != engine:
        raise ValueError("Selected instance does not belong to the requested engine")

    requested_engines = [engine] if engine else list(ENGINE_ORDER)
    catalog_items = []
    mode_counts: Counter[str] = Counter()
    availability_counts: Counter[str] = Counter()
    for capability in CAPABILITY_CATALOG:
        engine_cells = {}
        derived_category = DERIVED_MATURITY.get(capability["key"])
        if derived_category:
            from app.services.capability_registry import effective_maturity

            derived_maturity = effective_maturity(f"{capability['key']}.execute")
        else:
            derived_maturity = None
        if derived_maturity is None and capability["maturity"] is None:
            raise RuntimeError(f"Capability {capability['key']} declares no maturity source")
        for current_engine in requested_engines:
            mode = capability["modes"][current_engine]
            maturity = (
                derived_maturity[current_engine] if derived_maturity
                else capability["maturity"][current_engine]
            )
            engine_instances = [item for item in runtimes if item["engine"] == current_engine]
            runtime = selected if selected and selected["engine"] == current_engine else (engine_instances[0] if engine_instances else None)
            availability, availability_reason = _availability(mode, maturity, runtime, can_execute)
            ready_count = 0
            for item in engine_instances:
                item_status, _ = _availability(mode, maturity, item, can_execute)
                if item_status in {"available", "partial", "degraded", "plan_available"}:
                    ready_count += 1
            engine_cells[current_engine] = {
                "engine": current_engine,
                "engine_label": ENGINE_LABELS[current_engine],
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "maturity": maturity,
                "maturity_label": MATURITY_LABELS[maturity],
                "availability": availability,
                "availability_reason": availability_reason,
                "configured_instances": len(engine_instances),
                "ready_instances": ready_count,
            }
            mode_counts[mode] += 1
            availability_counts[availability] += 1
        catalog_items.append({
            key: value
            for key, value in capability.items()
            if key not in {"modes", "maturity"}
        } | {"engines": engine_cells})

    return {
        "engines": [{"key": item, "label": ENGINE_LABELS[item]} for item in requested_engines],
        "items": catalog_items,
        "instances": runtimes,
        "selected_instance": selected,
        "summary": {
            "capability_count": len(catalog_items),
            "engine_count": len(requested_engines),
            "configured_instance_count": len(runtimes),
            "mode_counts": dict(mode_counts),
            "availability_counts": dict(availability_counts),
            "execution_permission": bool(can_execute),
        },
    }

