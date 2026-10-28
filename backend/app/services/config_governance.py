from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.automation.models import ApprovalRequest, AutomationPlan, ExecutionOutput, ExecutionRun, RollbackPlan
from app.automation.policy import validate_maintenance_window
from app.automation.service import AutomationService
from app.cmdb.models import AssetInstance
from app.collectors.mysql.mysql_connection import mysql_connection
from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.postgresql.postgresql_connection import postgresql_connection
from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.core.database_options import redact_database_error
from app.models.config_governance import (
    DBConfigBaseline,
    DBConfigBaselineItem,
    DBConfigDrift,
    DBConfigGovernanceAudit,
    DBConfigRemediation,
    DBConfigSnapshot,
)
from app.models.db_instance import DBInstance


ACTIVE_DRIFT_STATUSES = {"open", "accepted", "remediation_planned", "pending_restart"}
STRICT_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.$]{0,127}$")
SQLSERVER_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_ .()/#-]{0,159}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config_binding(db: Session, remediation: DBConfigRemediation, instance: DBInstance) -> dict:
    baseline = db.get(DBConfigBaseline, remediation.baseline_id)
    snapshot = db.get(DBConfigSnapshot, remediation.snapshot_id)
    if instance.id != remediation.instance_id or not instance.enabled:
        raise ValueError("configuration target instance mismatch or disabled")
    if baseline is None or snapshot is None or (
        snapshot.instance_id != instance.id or snapshot.baseline_id != baseline.id
        or snapshot.baseline_version != baseline.version_no or snapshot.status != "success"
    ):
        raise ValueError("configuration baseline snapshot is stale or invalid")
    applicable, reason = baseline_applies(db, baseline, instance)
    if not applicable or baseline.status != "active":
        raise ValueError(reason or "configuration baseline is not active")
    baseline_data = baseline_detail(db, baseline)
    target = {name: getattr(instance, name) for name in (
        "id", "db_type", "db_version", "host", "port", "database_name", "username",
        "service_name", "sid", "certificate_profile_id", "tls_server_name", "tls_verify",
        "tenant_id", "organization_id",
    )}
    return {
        "target": target,
        "baseline_sha256": sha256_text(json.dumps(baseline_data, sort_keys=True, default=str)),
        "snapshot_id": snapshot.id,
        "operations": list(remediation.plan_json or []),
        "script_sha256": sha256_text(remediation.script),
        "rollback_sha256": sha256_text((remediation.rollback_script or "").strip()),
        "validation_sha256": sha256_text(remediation.validation_script),
    }


def validate_config_binding(db: Session, remediation: DBConfigRemediation, instance: DBInstance) -> dict:
    record = db.query(DBConfigGovernanceAudit).filter_by(
        entity_type="remediation", entity_id=remediation.id, action="execution_binding",
    ).one_or_none()
    current = _config_binding(db, remediation, instance)
    if record is None or record.after_json != current:
        raise ValueError("configuration binding changed; regenerate and approve a new remediation")
    if (remediation.script_sha256 != current["script_sha256"]
            or remediation.validation_sha256 != current["validation_sha256"]):
        raise ValueError("configuration script hash mismatch")
    return current


def serialize_model(row) -> dict[str, Any]:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat(sep=" ", timespec="seconds")
    return data


def baseline_detail(db: Session, baseline: DBConfigBaseline) -> dict[str, Any]:
    data = serialize_model(baseline)
    items = (
        db.query(DBConfigBaselineItem)
        .filter(DBConfigBaselineItem.baseline_id == baseline.id)
        .order_by(DBConfigBaselineItem.sort_order, DBConfigBaselineItem.id)
        .all()
    )
    data["items"] = [serialize_model(item) for item in items]
    data["item_count"] = len(items)
    return data


def audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    message: str | None = None,
) -> None:
    db.add(DBConfigGovernanceAudit(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        before_json=before,
        after_json=after,
        message=message,
    ))


def replace_baseline_items(
    db: Session,
    baseline: DBConfigBaseline,
    items: Iterable[Any],
) -> list[DBConfigBaselineItem]:
    values = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        item = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        key = (str(item.get("category") or "parameter").lower(), str(item["parameter_name"]).lower())
        if key in seen:
            raise ValueError(f"基线字段重复：{key[0]} / {item['parameter_name']}")
        seen.add(key)
        values.append(item)
    db.query(DBConfigBaselineItem).filter(DBConfigBaselineItem.baseline_id == baseline.id).delete()
    rows = [DBConfigBaselineItem(baseline_id=baseline.id, **item) for item in values]
    db.add_all(rows)
    return rows


def normalize_default_baseline(db: Session, baseline: DBConfigBaseline) -> None:
    if not baseline.is_default or baseline.status != "active":
        return
    query = db.query(DBConfigBaseline).filter(
        DBConfigBaseline.id != baseline.id,
        DBConfigBaseline.db_type == baseline.db_type,
        DBConfigBaseline.scope_type == baseline.scope_type,
        DBConfigBaseline.is_default == 1,
    )
    if baseline.scope_id is None:
        query = query.filter(DBConfigBaseline.scope_id.is_(None))
    else:
        query = query.filter(DBConfigBaseline.scope_id == baseline.scope_id)
    query.update({DBConfigBaseline.is_default: 0}, synchronize_session=False)


def baseline_applies(db: Session, baseline: DBConfigBaseline, instance: DBInstance) -> tuple[bool, str | None]:
    if baseline.status == "archived":
        return False, "基线已归档"
    if baseline.db_type != instance.db_type:
        return False, "基线数据库类型与实例不一致"
    version = str(instance.db_version or "")
    if baseline.version_pattern and not fnmatch.fnmatchcase(version, baseline.version_pattern):
        return False, f"实例版本 {version or '未知'} 不匹配 {baseline.version_pattern}"
    if baseline.scope_type == "instance" and baseline.scope_id != instance.id:
        return False, "基线仅适用于其他实例"
    if baseline.scope_type == "environment":
        normalized = (
            db.query(AssetInstance)
            .filter(AssetInstance.native_instance_id == instance.id)
            .order_by(AssetInstance.id.desc())
            .first()
        )
        if normalized is None or normalized.env_id != baseline.scope_id:
            return False, "实例不属于基线指定环境"
    return True, None


def _rows_as_dicts(cursor) -> list[dict[str, Any]]:
    rows = list(cursor.fetchall() or [])
    columns = [str(item[0]).lower() for item in (cursor.description or [])]
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append({str(key).lower(): value for key, value in row.items()})
        elif hasattr(row, "keys"):
            result.append({str(key).lower(): row[key] for key in row.keys()})
        else:
            result.append(dict(zip(columns, row)))
    return result


def _collect_with_cursor(connection, sql: str) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        return _rows_as_dicts(cursor)
    finally:
        cursor.close()


def collect_live_parameters(instance: DBInstance) -> dict[str, Any]:
    if instance.db_type == "oracle":
        with oracle_connection(instance, force_live=True, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as connection:
            rows = _collect_with_cursor(
                connection,
                "SELECT name, display_value AS value FROM v$parameter ORDER BY name",
            )
    elif instance.db_type == "mysql":
        with mysql_connection(instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as connection:
            rows = _collect_with_cursor(connection, "SHOW GLOBAL VARIABLES")
            for row in rows:
                if "variable_name" in row:
                    row["name"] = row.get("variable_name")
                    row["value"] = row.get("value") or row.get("variable_value")
    elif instance.db_type == "postgresql":
        with postgresql_connection(instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as connection:
            rows = _collect_with_cursor(connection, "SELECT name, setting AS value FROM pg_settings ORDER BY name")
    elif instance.db_type == "sqlserver":
        with sqlserver_connection(instance, read_only=True) as connection:
            rows = _collect_with_cursor(
                connection,
                "SELECT name, CAST(value_in_use AS NVARCHAR(4000)) AS value FROM sys.configurations ORDER BY name",
            )
    else:
        raise ValueError(f"不支持的数据库类型：{instance.db_type}")
    result: dict[str, Any] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name:
            result[name] = _json_scalar(row.get("value"))
    return result


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def normalize_observed(values: dict[str, Any] | None) -> dict[str, Any]:
    if not values:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict):
            normalized[str(key)] = {str(child): _json_scalar(child_value) for child, child_value in value.items()}
        else:
            normalized[str(key)] = _json_scalar(value)
    return normalized


def _observed_value(observed: dict[str, Any], category: str, name: str) -> Any:
    category_values = observed.get(category)
    if isinstance(category_values, dict) and name in category_values:
        return category_values[name]
    qualified = f"{category}.{name}"
    if qualified in observed:
        return observed[qualified]
    if category == "parameter":
        if name in observed:
            return observed[name]
        lowered = {str(key).lower(): value for key, value in observed.items() if not isinstance(value, dict)}
        return lowered.get(name.lower())
    return None


def _boolean(value: Any) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"无法转换为布尔值：{value}")


def _typed(value: Any, value_type: str) -> Any:
    if value_type == "integer":
        return int(str(value).strip())
    if value_type == "number":
        return Decimal(str(value).strip())
    if value_type == "boolean":
        return _boolean(value)
    return str(value).strip()


def compare_value(actual: Any, desired: str, comparator: str, value_type: str) -> bool:
    if actual is None:
        return False
    try:
        left = _typed(actual, value_type)
        if comparator == "in":
            try:
                choices = json.loads(desired)
                if not isinstance(choices, list):
                    choices = [choices]
            except json.JSONDecodeError:
                choices = [item.strip() for item in desired.split(",")]
            return left in [_typed(item, value_type) for item in choices]
        right = _typed(desired, value_type)
        if comparator == "eq":
            return left == right
        if comparator == "ne":
            return left != right
        if comparator == "ge":
            return left >= right
        if comparator == "le":
            return left <= right
        if comparator == "gt":
            return left > right
        if comparator == "lt":
            return left < right
        if comparator == "contains":
            return str(right) in str(left)
    except (InvalidOperation, TypeError, ValueError):
        return False
    raise ValueError(f"不支持的比较方式：{comparator}")


def evaluate_baseline(
    db: Session,
    baseline: DBConfigBaseline,
    instance: DBInstance,
    *,
    actor: str,
    source: str = "live",
    observed_parameters: dict[str, Any] | None = None,
) -> DBConfigSnapshot:
    applies, reason = baseline_applies(db, baseline, instance)
    if not applies:
        raise ValueError(reason or "基线不适用于当前实例")
    items = (
        db.query(DBConfigBaselineItem)
        .filter(DBConfigBaselineItem.baseline_id == baseline.id)
        .order_by(DBConfigBaselineItem.sort_order, DBConfigBaselineItem.id)
        .all()
    )
    if not items:
        raise ValueError("基线没有配置任何检查字段")

    collector_error = None
    if observed_parameters is None:
        try:
            observed = collect_live_parameters(instance)
        except Exception as exc:
            collector_error = redact_database_error(exc)
            observed = {}
    else:
        observed = normalize_observed(observed_parameters)
        source = "manual" if source == "live" else source

    expected = [serialize_model(item) for item in items]
    observed_payload = json.dumps(observed, ensure_ascii=False, sort_keys=True, default=str)
    snapshot = DBConfigSnapshot(
        instance_id=instance.id,
        baseline_id=baseline.id,
        baseline_version=baseline.version_no,
        source=source,
        status="failed" if collector_error else "success",
        observed_json=observed,
        expected_json=expected,
        observed_sha256=sha256_text(observed_payload),
        item_count=len(items),
        compliant_count=0,
        drift_count=0,
        missing_count=0,
        collector_error=collector_error,
        collected_by=actor,
    )
    db.add(snapshot)
    db.flush()
    if collector_error:
        audit(
            db,
            "snapshot",
            snapshot.id,
            "collection_failed",
            actor,
            after=serialize_model(snapshot),
            message=collector_error,
        )
        db.commit()
        db.refresh(snapshot)
        return snapshot

    existing_rows = (
        db.query(DBConfigDrift)
        .filter(DBConfigDrift.instance_id == instance.id, DBConfigDrift.baseline_id == baseline.id)
        .all()
    )
    existing = {(row.category.lower(), row.parameter_name.lower()): row for row in existing_rows}
    mismatches: set[tuple[str, str]] = set()
    now = utcnow()
    missing_count = 0
    compliant_count = 0
    for item in items:
        actual = _observed_value(observed, item.category, item.parameter_name)
        compliant = compare_value(actual, item.desired_value, item.comparator, item.value_type)
        key = (item.category.lower(), item.parameter_name.lower())
        if compliant:
            compliant_count += 1
            continue
        mismatches.add(key)
        if actual is None:
            missing_count += 1
        row = existing.get(key)
        actual_text = None if actual is None else str(actual)
        if row is None:
            row = DBConfigDrift(
                instance_id=instance.id,
                baseline_id=baseline.id,
                snapshot_id=snapshot.id,
                baseline_item_id=item.id,
                category=item.category,
                parameter_name=item.parameter_name,
                comparator=item.comparator,
                desired_value=item.desired_value,
                actual_value=actual_text,
                severity=item.severity,
                restart_required=item.restart_required,
                status="open",
                first_detected_at=now,
                last_detected_at=now,
            )
            db.add(row)
        else:
            unchanged_accepted = (
                row.status == "accepted"
                and row.actual_value == actual_text
                and row.desired_value == item.desired_value
                and row.comparator == item.comparator
            )
            keep_plan = row.status in {"remediation_planned", "pending_restart"} and row.desired_value == item.desired_value
            row.snapshot_id = snapshot.id
            row.baseline_item_id = item.id
            row.comparator = item.comparator
            row.desired_value = item.desired_value
            row.actual_value = actual_text
            row.severity = item.severity
            row.restart_required = item.restart_required
            row.last_detected_at = now
            row.verified_at = None
            if not unchanged_accepted and not keep_plan:
                row.status = "open"
                row.resolution_reason = None
                row.accepted_by = None
                row.accepted_at = None

    for key, row in existing.items():
        if key not in mismatches and row.status in ACTIVE_DRIFT_STATUSES:
            row.status = "resolved"
            row.verified_at = now
            row.resolution_reason = "复核值已符合基线"

    snapshot.compliant_count = compliant_count
    snapshot.drift_count = len(mismatches)
    snapshot.missing_count = missing_count
    audit(
        db,
        "snapshot",
        snapshot.id,
        "evaluated",
        actor,
        after={
            "instance_id": instance.id,
            "baseline_id": baseline.id,
            "compliant_count": compliant_count,
            "drift_count": len(mismatches),
            "missing_count": missing_count,
        },
        message="配置基线比对完成",
    )
    db.commit()
    db.refresh(snapshot)
    return snapshot


def accept_drift(db: Session, drift: DBConfigDrift, *, actor: str, reason: str) -> DBConfigDrift:
    if drift.status not in {"open", "accepted"}:
        raise ValueError("只有待处理漂移可以接受为例外")
    before = serialize_model(drift)
    drift.status = "accepted"
    drift.accepted_by = actor
    drift.accepted_at = utcnow()
    drift.resolution_reason = reason
    audit(db, "drift", drift.id, "accepted", actor, before=before, after=serialize_model(drift), message=reason)
    db.commit()
    db.refresh(drift)
    return drift


def _sql_string(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _numeric_or_boolean(value: str, value_type: str) -> str:
    if value_type == "integer":
        return str(int(value))
    if value_type == "number":
        return str(Decimal(value))
    if value_type == "boolean":
        return "1" if _boolean(value) else "0"
    return _sql_string(value)


def _parameter_statement(db_type: str, name: str, value: str, value_type: str, restart_required: bool) -> str:
    if db_type != "sqlserver" and not STRICT_PARAMETER_NAME.fullmatch(name):
        raise ValueError(f"参数名不安全或不受支持：{name}")
    if db_type == "sqlserver" and not SQLSERVER_PARAMETER_NAME.fullmatch(name):
        raise ValueError(f"SQL Server 参数名不安全或不受支持：{name}")
    if db_type == "oracle":
        scope = "SPFILE" if restart_required else "BOTH"
        return f"ALTER SYSTEM SET {name} = {_numeric_or_boolean(value, value_type)} SCOPE={scope};"
    if db_type == "mysql":
        return f"SET PERSIST {name} = {_numeric_or_boolean(value, value_type)};"
    if db_type == "postgresql":
        return f"ALTER SYSTEM SET {name} TO {_sql_string(value)};"
    if db_type == "sqlserver":
        if value_type not in {"integer", "number", "boolean"}:
            raise ValueError(f"SQL Server sp_configure 仅支持数值型基线：{name}")
        return f"EXEC sp_configure N{_sql_string(name)}, {_numeric_or_boolean(value, value_type)}; RECONFIGURE;"
    raise ValueError(f"不支持的数据库类型：{db_type}")


def _validation_script(db_type: str, names: list[str]) -> str:
    safe_names = [_sql_string(name) for name in names]
    values = ", ".join(safe_names) or "''"
    if db_type == "oracle":
        return f"SELECT name, display_value AS actual_value FROM v$parameter WHERE name IN ({values}) ORDER BY name;"
    if db_type == "mysql":
        return f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({values});"
    if db_type == "postgresql":
        return f"SELECT name, setting AS actual_value, pending_restart FROM pg_settings WHERE name IN ({values}) ORDER BY name;"
    return f"SELECT name, value_in_use AS actual_value FROM sys.configurations WHERE name IN ({values}) ORDER BY name;"


def _build_plan(
    instance: DBInstance,
    drifts: list[DBConfigDrift],
    items: dict[int, DBConfigBaselineItem],
) -> tuple[list[dict[str, Any]], str, str, str]:
    operations: list[dict[str, Any]] = []
    script_lines = [
        f"-- TraceMind configuration remediation for {instance.instance_name}",
        f"-- Target: {instance.db_type} {instance.host}:{instance.port}",
        "-- Generated plan requires approval; review every statement before execution.",
    ]
    rollback_lines = [
        f"-- Rollback for TraceMind configuration remediation on {instance.instance_name}",
    ]
    parameter_names: list[str] = []
    for drift in drifts:
        item = items.get(int(drift.baseline_item_id or 0))
        value_type = item.value_type if item else "string"
        operation = {
            "drift_id": drift.id,
            "category": drift.category,
            "parameter_name": drift.parameter_name,
            "desired_value": drift.desired_value,
            "actual_value": drift.actual_value,
            "value_type": value_type,
            "restart_required": bool(drift.restart_required),
            "executable": False,
        }
        if drift.category != "parameter":
            hint = item.remediation_hint if item else None
            statement = f"-- Manual {drift.category}: {drift.parameter_name} -> {drift.desired_value}"
            if hint:
                statement += f" | {hint}"
        else:
            statement = _parameter_statement(
                instance.db_type,
                drift.parameter_name,
                drift.desired_value,
                value_type,
                bool(drift.restart_required),
            )
            operation["executable"] = True
            parameter_names.append(drift.parameter_name)
            if drift.actual_value is not None:
                try:
                    rollback_lines.append(_parameter_statement(
                        instance.db_type,
                        drift.parameter_name,
                        drift.actual_value,
                        value_type,
                        bool(drift.restart_required),
                    ))
                except ValueError:
                    rollback_lines.append(f"-- Rollback requires manual value for {drift.parameter_name}")
        operation["statement_preview"] = statement
        operations.append(operation)
        script_lines.append(statement)
    if instance.db_type == "postgresql" and parameter_names:
        script_lines.append("SELECT pg_reload_conf();")
        rollback_lines.append("SELECT pg_reload_conf();")
    rollback_script = "\n".join(rollback_lines) if len(rollback_lines) > 1 else ""
    return operations, "\n".join(script_lines), rollback_script, _validation_script(instance.db_type, parameter_names)


def create_remediation(
    db: Session,
    baseline: DBConfigBaseline,
    instance: DBInstance,
    *,
    actor: str,
    drift_ids: list[int] | None = None,
) -> DBConfigRemediation:
    query = db.query(DBConfigDrift).filter(
        DBConfigDrift.instance_id == instance.id,
        DBConfigDrift.baseline_id == baseline.id,
        DBConfigDrift.status.in_(["open", "accepted", "remediation_planned", "pending_restart"]),
    )
    if drift_ids:
        query = query.filter(DBConfigDrift.id.in_(set(drift_ids)))
    drifts = query.order_by(DBConfigDrift.severity.desc(), DBConfigDrift.id).all()
    if not drifts:
        raise ValueError("没有可生成整改方案的活动漂移")
    item_ids = {int(row.baseline_item_id) for row in drifts if row.baseline_item_id}
    items = {
        row.id: row
        for row in db.query(DBConfigBaselineItem).filter(DBConfigBaselineItem.id.in_(item_ids or {-1})).all()
    }
    operations, script, rollback_script, validation_script = _build_plan(instance, drifts, items)
    risk = "high" if any(row.severity == "critical" or row.restart_required for row in drifts) else "medium"
    snapshot_id = max(row.snapshot_id for row in drifts)
    remediation = DBConfigRemediation(
        instance_id=instance.id,
        baseline_id=baseline.id,
        snapshot_id=snapshot_id,
        drift_ids_json=[row.id for row in drifts],
        plan_json=operations,
        script=script,
        script_sha256=sha256_text(script),
        rollback_script=rollback_script,
        validation_script=validation_script,
        validation_sha256=sha256_text(validation_script),
        risk_level=risk,
        execution_mode="approval_required",
        status="draft",
        created_by=actor,
    )
    db.add(remediation)
    db.flush()
    audit(db, "remediation", remediation.id, "execution_binding", actor,
          after=_config_binding(db, remediation, instance))
    for drift in drifts:
        if drift.status != "accepted":
            drift.status = "remediation_planned"
    audit(
        db,
        "remediation",
        remediation.id,
        "created",
        actor,
        after={"drift_ids": remediation.drift_ids_json, "risk_level": risk, "script_sha256": remediation.script_sha256},
        message="已生成只读整改预案，等待审批",
    )
    db.commit()
    db.refresh(remediation)
    return remediation


def submit_remediation(
    db: Session,
    remediation: DBConfigRemediation,
    instance: DBInstance,
    *,
    actor: str,
    expected_target_identity: str | None = None,
    maintenance_window_start: datetime | None = None,
    maintenance_window_end: datetime | None = None,
    rollback_script: str | None = None,
) -> DBConfigRemediation:
    validate_config_binding(db, remediation, instance)
    if remediation.approval_request_id:
        return remediation
    if remediation.status not in {"draft", "approval_failed"}:
        raise ValueError("当前整改方案不能提交审批")
    if remediation.risk_level == "high" and (not maintenance_window_start or not maintenance_window_end):
        raise ValueError("高风险整改必须设置维护窗口")
    effective_rollback = (rollback_script or remediation.rollback_script or "").strip()
    if remediation.risk_level == "high" and not effective_rollback:
        raise ValueError("高风险整改必须提供可执行的回滚脚本")
    service = AutomationService(db)
    plan = service.create_plan(
        actor=actor,
        idempotency_key=f"config-remediation:{remediation.id}",
        plan_kind="configuration_change",
        name=f"配置漂移整改：{instance.instance_name}",
        script=remediation.script,
        script_sha256=remediation.script_sha256,
        target_host=instance.host,
        host_whitelist=[instance.host],
        executor_type="database",
        risk_level=remediation.risk_level,
        maintenance_window_start=maintenance_window_start,
        maintenance_window_end=maintenance_window_end,
        timeout_seconds=300,
        max_retries=0,
        retry_interval_seconds=10,
        expected_target_identity=expected_target_identity or f"{instance.db_type}:{instance.host}:{instance.port}",
        validation_script=remediation.validation_script,
        validation_sha256=remediation.validation_sha256,
    )
    existing_rollback = None
    if effective_rollback:
        existing_rollback = db.query(RollbackPlan).filter(RollbackPlan.plan_id == plan.id).first()
        if existing_rollback is None:
            existing_rollback = service.create_rollback_plan(
                plan.id, effective_rollback, sha256_text(effective_rollback), actor,
            )
    request = service.submit_plan(plan.id, actor)
    remediation.automation_plan_id = plan.id
    remediation.approval_request_id = request.id
    remediation.status = "pending_approval"
    audit(
        db,
        "remediation",
        remediation.id,
        "submitted",
        actor,
        after={"automation_plan_id": plan.id, "approval_request_id": request.id,
               "rollback_plan_id": existing_rollback.id if existing_rollback else None},
        message="配置整改已进入统一审批中心",
    )
    db.commit()
    db.refresh(remediation)
    return remediation


def _execute_parameter_operations(instance: DBInstance, operations: list[dict[str, Any]]) -> list[str]:
    executable = [item for item in operations if item.get("category") == "parameter" and item.get("executable")]
    messages: list[str] = []
    if not executable:
        return ["没有可自动执行的参数项；非参数治理项需按方案人工处理"]

    def run(connection) -> None:
        cursor = connection.cursor()
        try:
            for item in executable:
                statement = _parameter_statement(
                    instance.db_type,
                    str(item["parameter_name"]),
                    str(item["desired_value"]),
                    str(item.get("value_type") or "string"),
                    bool(item.get("restart_required")),
                )
                cursor.execute(statement)
                messages.append(f"已提交参数 {item['parameter_name']}")
            if instance.db_type == "postgresql":
                cursor.execute("SELECT pg_reload_conf()")
            if hasattr(connection, "commit"):
                connection.commit()
        finally:
            cursor.close()

    if instance.db_type == "oracle":
        with oracle_connection(instance, force_live=True, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as connection:
            run(connection)
    elif instance.db_type == "mysql":
        with mysql_connection(instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as connection:
            run(connection)
    elif instance.db_type == "postgresql":
        with postgresql_connection(instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as connection:
            connection.autocommit = True
            run(connection)
    elif instance.db_type == "sqlserver":
        with sqlserver_connection(instance) as connection:
            run(connection)
    else:
        raise ValueError(f"不支持的数据库类型：{instance.db_type}")
    manual_count = len(operations) - len(executable)
    if manual_count:
        messages.append(f"{manual_count} 项非参数治理动作保留为人工步骤")
    return messages


def apply_remediation(
    db: Session,
    remediation_id: int,
    instance: DBInstance,
    *,
    actor: str,
    confirm: bool,
) -> DBConfigRemediation:
    if not confirm:
        raise ValueError("必须明确确认后才能执行已审批整改")
    remediation = (
        db.query(DBConfigRemediation)
        .filter(DBConfigRemediation.id == remediation_id)
        .with_for_update()
        .first()
    )
    if remediation is None:
        raise ValueError("整改方案不存在")
    validate_config_binding(db, remediation, instance)
    if remediation.status.startswith("rollback") or remediation.status == "rolled_back":
        raise ValueError("rolled back remediation requires a new approved plan")
    if remediation.status in {"applying", "applied", "pending_restart", "verified"}:
        if remediation.status == "applying":
            raise ValueError("整改方案正在执行")
        return remediation
    request = db.get(ApprovalRequest, remediation.approval_request_id)
    plan = db.get(AutomationPlan, remediation.automation_plan_id)
    if request is None or plan is None or request.status != "approved" or plan.status != "approved":
        raise ValueError("整改方案尚未通过统一审批")
    expected_identity = f"{instance.db_type}:{instance.host}:{instance.port}"
    if plan.expected_target_identity and plan.expected_target_identity != expected_identity:
        raise ValueError("目标实例身份与审批方案不一致")
    validate_maintenance_window(plan)
    run_key = f"config-remediation:{remediation.id}:apply"
    existing_run = db.query(ExecutionRun).filter(ExecutionRun.idempotency_key == run_key).first()
    if existing_run and existing_run.status == "success":
        return remediation
    run = existing_run or ExecutionRun(
        idempotency_key=run_key,
        request_id=request.id,
        plan_id=plan.id,
        executor_id=actor,
        executor_type="database",
        status="running",
        attempt_count=1,
        target_identity=expected_identity,
    )
    if existing_run is None:
        db.add(run)
    else:
        run.status = "running"
        run.attempt_count = int(run.attempt_count or 0) + 1
        run.error_message = None
        run.finished_at = None
    remediation.status = "applying"
    request.status = "executing"
    plan.status = "executing"
    db.commit()
    try:
        messages = _execute_parameter_operations(instance, list(remediation.plan_json or []))
    except Exception as exc:
        error = redact_database_error(exc)
        remediation = db.get(DBConfigRemediation, remediation.id)
        request = db.get(ApprovalRequest, request.id)
        plan = db.get(AutomationPlan, plan.id)
        run = db.query(ExecutionRun).filter(ExecutionRun.idempotency_key == run_key).first()
        remediation.status = "failed"
        remediation.apply_message = error
        request.status = "approved"
        plan.status = "approved"
        run.status = "failed"
        run.error_message = error
        run.finished_at = utcnow()
        output = db.query(ExecutionOutput).filter(ExecutionOutput.run_id == run.id).first()
        if output is None:
            output = ExecutionOutput(run_id=run.id, stdout="", stderr=error, output_sha256=sha256_text(error))
            db.add(output)
        else:
            output.stdout = ""
            output.stderr = error
            output.output_sha256 = sha256_text(error)
        audit(db, "remediation", remediation.id, "apply_failed", actor, message=error)
        db.commit()
        db.refresh(remediation)
        return remediation

    remediation = db.get(DBConfigRemediation, remediation.id)
    request = db.get(ApprovalRequest, request.id)
    plan = db.get(AutomationPlan, plan.id)
    run = db.query(ExecutionRun).filter(ExecutionRun.idempotency_key == run_key).first()
    message = "；".join(messages)
    has_restart = any(bool(item.get("restart_required")) for item in (remediation.plan_json or []))
    remediation.status = "pending_restart" if has_restart else "pending_verification"
    remediation.apply_message = message
    remediation.applied_by = actor
    remediation.applied_at = utcnow()
    request.status = "executed"
    request.executed_at = utcnow()
    plan.status = "executed"
    run.status = "success"
    run.exit_code = 0
    run.validation_status = "pending"
    run.validation_message = "等待实时复核"
    run.finished_at = utcnow()
    output = db.query(ExecutionOutput).filter(ExecutionOutput.run_id == run.id).first()
    if output is None:
        output = ExecutionOutput(run_id=run.id, stdout=message, stderr="", output_sha256=sha256_text(message))
        db.add(output)
    else:
        output.stdout = message
        output.stderr = ""
        output.output_sha256 = sha256_text(message)
    for drift in db.query(DBConfigDrift).filter(DBConfigDrift.id.in_(remediation.drift_ids_json or {-1})).all():
        drift.status = "pending_restart" if drift.restart_required else "remediation_planned"
    audit(
        db,
        "remediation",
        remediation.id,
        "applied",
        actor,
        after={"status": remediation.status, "run_id": run.id},
        message=message,
    )
    db.commit()
    db.refresh(remediation)
    return remediation


def verify_remediation(
    db: Session,
    remediation: DBConfigRemediation,
    baseline: DBConfigBaseline,
    instance: DBInstance,
    *,
    actor: str,
    observed_parameters: dict[str, Any] | None = None,
) -> tuple[DBConfigRemediation, DBConfigSnapshot]:
    if remediation.status not in {"pending_verification", "pending_restart", "failed", "applied"}:
        raise ValueError("当前整改方案不在待复核状态")
    snapshot = evaluate_baseline(
        db,
        baseline,
        instance,
        actor=actor,
        source="live" if observed_parameters is None else "manual",
        observed_parameters=observed_parameters,
    )
    remediation = db.get(DBConfigRemediation, remediation.id)
    if snapshot.status == "failed":
        remediation.status = "verification_failed"
        remediation.apply_message = snapshot.collector_error
    else:
        rows = db.query(DBConfigDrift).filter(DBConfigDrift.id.in_(remediation.drift_ids_json or {-1})).all()
        remaining = [row for row in rows if row.status != "resolved"]
        if remaining:
            remediation.status = "pending_restart" if any(row.restart_required for row in remaining) else "drift_remaining"
            remediation.apply_message = f"复核后仍有 {len(remaining)} 项漂移"
        else:
            remediation.status = "verified"
            remediation.verified_by = actor
            remediation.verified_at = utcnow()
            remediation.apply_message = "全部整改字段已通过实时复核"
            run = (
                db.query(ExecutionRun)
                .filter(ExecutionRun.plan_id == remediation.automation_plan_id)
                .order_by(ExecutionRun.id.desc())
                .first()
            )
            if run:
                run.validation_status = "success"
                run.validation_message = remediation.apply_message
    audit(
        db,
        "remediation",
        remediation.id,
        "verified",
        actor,
        after={"status": remediation.status, "snapshot_id": snapshot.id},
        message=remediation.apply_message,
    )
    db.commit()
    db.refresh(remediation)
    return remediation, snapshot


def summary(db: Session, *, allowed_instance_ids: set[int] | frozenset[int] | None = None) -> dict[str, Any]:
    """Aggregate baselines/drifts/compliance for the caller's instance scope.

    ``None`` means unrestricted; a restricted caller's empty set must stay empty rather
    than collapsing into "all instances".
    """
    instances_query = db.query(DBInstance)
    if allowed_instance_ids is not None:
        instances_query = instances_query.filter(DBInstance.id.in_(allowed_instance_ids or {-1}))
    instances = instances_query.all()
    instance_ids = {row.id for row in instances}
    drift_query = db.query(DBConfigDrift)
    snapshot_query = db.query(DBConfigSnapshot)
    remediation_query = db.query(DBConfigRemediation)
    if allowed_instance_ids is not None:
        drift_query = drift_query.filter(DBConfigDrift.instance_id.in_(instance_ids or {-1}))
        snapshot_query = snapshot_query.filter(DBConfigSnapshot.instance_id.in_(instance_ids or {-1}))
        remediation_query = remediation_query.filter(DBConfigRemediation.instance_id.in_(instance_ids or {-1}))
    drifts = drift_query.all()
    latest = snapshot_query.order_by(DBConfigSnapshot.collected_at.desc(), DBConfigSnapshot.id.desc()).limit(200).all()
    latest_by_instance: dict[int, DBConfigSnapshot] = {}
    for row in latest:
        latest_by_instance.setdefault(row.instance_id, row)
    successful = [row for row in latest_by_instance.values() if row.status == "success"]
    checked = sum(row.item_count for row in successful)
    compliant = sum(row.compliant_count for row in successful)
    engine_counts = {engine: 0 for engine in ("oracle", "mysql", "postgresql", "sqlserver")}
    for instance in instances:
        if instance.db_type in engine_counts:
            engine_counts[instance.db_type] += 1
    return {
        "baseline_count": db.query(DBConfigBaseline).filter(DBConfigBaseline.status != "archived").count(),
        "active_baseline_count": db.query(DBConfigBaseline).filter(DBConfigBaseline.status == "active").count(),
        "scoped_instance_count": len(instances),
        "evaluated_instance_count": len(latest_by_instance),
        "collection_failure_count": len([row for row in latest_by_instance.values() if row.status == "failed"]),
        "active_drift_count": len([row for row in drifts if row.status in ACTIVE_DRIFT_STATUSES]),
        "critical_drift_count": len([
            row for row in drifts
            if row.status in ACTIVE_DRIFT_STATUSES and row.severity == "critical"
        ]),
        "pending_remediation_count": remediation_query.filter(
            DBConfigRemediation.status.notin_(["verified", "cancelled"])
        ).count(),
        "compliance_percent": round(compliant * 100 / checked, 2) if checked else None,
        "engine_counts": engine_counts,
    }
