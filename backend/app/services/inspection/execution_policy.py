"""Resolve and freeze the same template for manual, scheduled and legacy callers."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.oracle.oracle_inspection_profile import detect_oracle_profile, SCRIPT_CATALOG, SCRIPT_DIR
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.inspection_policy import DBInspectionPolicy
from . import policy_service, template_runtime
from .host_platform import detect_host_platform
from .template_profiles import (
    ORACLE_TEMPLATE_KEYS,
    normalize_profile_config,
    require_matching_oracle_profile,
)


def resolve_execution_policy(db, instance, policy=None, *, frozen=False):
    policy_service.ensure_default_policies(db)
    # Refresh before execution; later commits must not change an in-flight template.
    rows = db.query(DBInspectionPolicy).populate_existing().order_by(DBInspectionPolicy.id).all()
    if policy is not None and not frozen:
        policy_id = getattr(policy, "id", None)
        key = getattr(policy, "policy_key", None)
        policy = next((row for row in rows if row.id == policy_id or row.policy_key == key), None)
        if policy is None:
            raise ValueError("巡检模板不存在，未执行巡检")
    applicable = [row for row in rows if instance.db_type in (row.db_types_json or [])]
    automatic = policy is None

    if policy is not None:
        if not policy.enabled:
            raise ValueError(f"巡检模板“{policy.name}”已停用，未执行巡检")
        if instance.db_type not in (policy.db_types_json or []):
            raise ValueError(f"巡检模板“{policy.name}”不适用于 {instance.db_type}")
        template_runtime.require_template_runtime(policy.db_types_json, policy.item_config_json, policy.report_config_json)

    oracle_profile = None
    host_platform = None
    if instance.db_type == "oracle":
        with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
            detected = detect_oracle_profile(conn)
        expected = (getattr(policy, "report_config_json", None) or {}).get("oracle_profile")
        oracle_profile = require_matching_oracle_profile(detected, expected)
    elif instance.db_type == "host":
        host_platform = detect_host_platform(instance)
        expected = (getattr(policy, "report_config_json", None) or {}).get("host_platform", "auto")
        if expected not in ("auto", host_platform):
            raise ValueError(f"主机模板不匹配：模板适用 {expected}，实际为 {host_platform}；未执行巡检")

    if automatic:
        defaults = [row for row in applicable if instance.db_type in policy_service._assistant_default_db_types(row)]
        if oracle_profile:
            defaults = [row for row in defaults if (row.report_config_json or {}).get("oracle_profile", "auto") in ("auto", oracle_profile)]
        if host_platform:
            defaults = [row for row in defaults if (row.report_config_json or {}).get("host_platform", "auto") in ("auto", host_platform)]
        if len(defaults) > 1:
            raise ValueError("当前数据库匹配到多个默认巡检模板，请先消除默认配置冲突")
        policy = next(iter(defaults), None)
    if policy is None:
        default_key = ORACLE_TEMPLATE_KEYS[oracle_profile] if oracle_profile else (
            ("host_windows" if host_platform == "windows" else "host_resource") if instance.db_type == "host" else f"{instance.db_type}_standard"
        )
        policy = next((row for row in applicable if row.policy_key == default_key), None)
    if policy is None:
        raise ValueError("未配置可用的巡检模板，未执行巡检")
    if not policy.enabled:
        raise ValueError(f"巡检模板“{policy.name}”已停用，未执行巡检")
    if instance.db_type not in (policy.db_types_json or []):
        raise ValueError(f"巡检模板“{policy.name}”不适用于 {instance.db_type}")
    report_config = normalize_profile_config(policy.db_types_json, policy.report_config_json)
    if oracle_profile and report_config.get("oracle_profile", "auto") == "auto":
        report_config = normalize_profile_config(policy.db_types_json, {**report_config, "oracle_profile": oracle_profile})
    if host_platform and report_config.get("host_platform", "auto") == "auto":
        report_config = normalize_profile_config(policy.db_types_json, {**report_config, "host_platform": host_platform})
    template_runtime.require_template_runtime(policy.db_types_json, policy.item_config_json, report_config)
    fields = (
        "id", "policy_key", "name", "db_types_json", "enabled", "item_config_json",
        "threshold_config_json", "action_config_json", "report_config_json", "update_time",
    )
    snapshot = SimpleNamespace(**{key: deepcopy(getattr(policy, key)) for key in fields})
    snapshot.report_config_json = report_config
    snapshot.resolved_oracle_profile = oracle_profile
    snapshot.resolved_host_platform = host_platform
    snapshot.selection_mode = "automatic" if automatic else "explicit"
    script_name = SCRIPT_CATALOG[oracle_profile]["script_name"] if oracle_profile else {
        "mysql": "mysql_check.sh", "postgresql": "pg_check.sh",
        "sqlserver": "mssql_check.sh", "host": "oscheck.ps1" if host_platform == "windows" else "oscheck.sh",
    }.get(instance.db_type)
    snapshot.source_script = None
    if script_name and script_name in {
        "dbcheck10g.sql", "dbcheck_noncdb.sql", "dbcheck_cdb.sql",
        "mysql_check.sh", "pg_check.sh", "mssql_check.sh", "oscheck.sh", "oscheck.ps1",
    }:
        path = SCRIPT_DIR / script_name
        if oracle_profile and not path.is_file():
            raise ValueError(f"缺少巡检脚本：{script_name}，未执行巡检")
        snapshot.source_script = {
            "name": script_name,
            "sha256": sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            "mode": "parsed_sql" if oracle_profile else "collector_reference",
        }
    return snapshot
