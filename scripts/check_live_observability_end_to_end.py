#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter
from typing import Any

from _bootstrap import add_backend_to_path

os.environ.setdefault("DEBUG", "false")
add_backend_to_path()

from app.core.database import SessionLocal
from app.models.alert_event import DBAlertActionLog, DBAlertEvent
from app.models.db_instance import DBInstance
from app.models.external_monitoring import ZabbixHostMapping
from app.models.observability import (
    ObservabilityAlertEvent,
    ObservabilityInstanceBinding,
    ObservabilityMetricHistory,
)
from app.services.diagnosis import constants as C
from app.services.diagnosis import evidence_chain as diagnosis_evidence_chain
from app.services.observability import alert_diagnosis_service, alert_ingest_service


@dataclass(frozen=True)
class Case:
    host: str
    source_type: str
    name: str
    source_metric: str
    expected_metric: str
    expected_category: str
    expected_event_layer: str
    expected_diag_layer: str
    value: Decimal
    title: str
    content: str
    severity: str = "warning"


CASES = [
    Case(
        host="oracle",
        source_type="prometheus",
        name="oracle_prom_fra",
        source_metric="oracle_fra_used_pct",
        expected_metric="oracle.fra.used_pct",
        expected_category="oracle_fra",
        expected_event_layer="database",
        expected_diag_layer=C.LAYER_DB,
        value=Decimal("96.2"),
        title="MESGuardE2EOracleFRAHigh",
        content="Oracle FRA usage is 96.2 percent and archive cleanup is required.",
        severity="critical",
    ),
    Case(
        host="oracle",
        source_type="zabbix",
        name="oracle_zbx_tablespace",
        source_metric="db.tablespace.used_pct",
        expected_metric="db.tablespace.used_pct",
        expected_category="tablespace_capacity",
        expected_event_layer="database",
        expected_diag_layer=C.LAYER_DB,
        value=Decimal("93.4"),
        title="MESGuardE2EOracleTablespaceHigh",
        content="Oracle tablespace USERS used percent is 93.4.",
        severity="critical",
    ),
    Case(
        host="mysql",
        source_type="prometheus",
        name="mysql_prom_replication",
        source_metric="mysql_replication_lag_seconds",
        expected_metric="mysql.replication.lag_seconds",
        expected_category="replication",
        expected_event_layer="database",
        expected_diag_layer=C.LAYER_DB,
        value=Decimal("420"),
        title="MESGuardE2EMySQLReplicationLag",
        content="MySQL replication lag is 420 seconds.",
        severity="critical",
    ),
    Case(
        host="mysql",
        source_type="zabbix",
        name="mysql_zbx_memory",
        source_metric="vm.memory.size[pused]",
        expected_metric="host.memory.used_pct",
        expected_category="memory",
        expected_event_layer="os",
        expected_diag_layer=C.LAYER_OS,
        value=Decimal("88.3"),
        title="MESGuardE2EMySQLMemoryPressure",
        content="Zabbix reports MySQL host memory used percent is 88.3.",
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cleanup(db, prefix: str) -> dict[str, int]:
    event_ids = [
        row.id
        for row in db.query(DBAlertEvent)
        .filter(DBAlertEvent.event_uid.like(f"%{prefix}%"))
        .all()
    ]
    deleted: dict[str, int] = {}
    if event_ids:
        deleted["action_log"] = db.query(DBAlertActionLog).filter(DBAlertActionLog.event_id.in_(event_ids)).delete(
            synchronize_session=False
        )
    deleted["db_alert_event"] = db.query(DBAlertEvent).filter(DBAlertEvent.event_uid.like(f"%{prefix}%")).delete(
        synchronize_session=False
    )
    deleted["observability_alert_event"] = db.query(ObservabilityAlertEvent).filter(
        ObservabilityAlertEvent.alert_key.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    deleted["metric_history"] = db.query(ObservabilityMetricHistory).filter(
        ObservabilityMetricHistory.host_id.like(f"%{prefix}%")
    ).delete(synchronize_session=False)
    db.commit()
    return deleted


def _instance(db, host: str) -> DBInstance:
    row = db.query(DBInstance).filter(DBInstance.host == host, DBInstance.enabled == 1).first()
    if not row:
        raise RuntimeError(f"No enabled DB instance found for host {host}")
    return row


def _binding(db, instance: DBInstance) -> ObservabilityInstanceBinding | None:
    rows = (
        db.query(ObservabilityInstanceBinding)
        .filter(ObservabilityInstanceBinding.instance_id == instance.id, ObservabilityInstanceBinding.enabled == 1)
        .order_by(ObservabilityInstanceBinding.confidence.desc(), ObservabilityInstanceBinding.id.desc())
        .all()
    )
    real_rows = [row for row in rows if not _is_test_binding(row)]
    return (real_rows or rows or [None])[0]


def _is_test_binding(row: ObservabilityInstanceBinding) -> bool:
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    return (
        str(row.match_rule or "").startswith("test_")
        or "test_prefix" in raw
        or str(row.host_identifier or "").startswith(("codex-", "test-"))
        or str(row.zabbix_hostid or "").startswith(("codex-", "test-"))
        or str(row.prometheus_target or "").startswith(("codex-", "test-"))
    )


def _zabbix_hostid(db, instance: DBInstance, binding: ObservabilityInstanceBinding | None) -> str:
    if binding and binding.zabbix_hostid:
        return str(binding.zabbix_hostid)
    mapping = (
        db.query(ZabbixHostMapping)
        .filter(ZabbixHostMapping.db_instance_id == instance.id, ZabbixHostMapping.enabled == 1)
        .order_by(ZabbixHostMapping.id.desc())
        .first()
    )
    if mapping and mapping.zabbix_hostid:
        return str(mapping.zabbix_hostid)
    raise RuntimeError(f"No Zabbix host mapping found for instance {instance.id}")


def _prometheus_target(instance: DBInstance, binding: ObservabilityInstanceBinding | None) -> str:
    if binding and binding.prometheus_target:
        return str(binding.prometheus_target)
    return str(instance.host)


def _labels(db, instance: DBInstance, case: Case) -> tuple[str, dict[str, str]]:
    binding = _binding(db, instance)
    if case.source_type == "prometheus":
        host_id = _prometheus_target(instance, binding)
        return host_id, {
            "alertname": case.title,
            "instance": host_id,
            "host": instance.host or host_id,
            "db_type": instance.db_type or "",
            "severity": case.severity,
            "metric": case.source_metric,
        }
    host_id = _zabbix_hostid(db, instance, binding)
    return host_id, {
        "hostid": host_id,
        "host": instance.host or host_id,
        "db_type": instance.db_type or "",
        "severity": case.severity,
        "metric": case.source_metric,
    }


def _event_for_case(db, source_type: str, alert_key: str) -> DBAlertEvent:
    event_uid = f"observability:{source_type}:{alert_key}"
    row = db.query(DBAlertEvent).filter(DBAlertEvent.event_uid == event_uid).first()
    if not row:
        raise RuntimeError(f"Unified event not found: {event_uid}")
    return row


def _insert_metric_history(db, case: Case, event: DBAlertEvent, host_id: str) -> None:
    center = event.last_seen or _now()
    alert_key = event.event_uid.split(":", 2)[-1]
    metric_host_id = f"{alert_key}:{host_id}"
    if len(metric_host_id) > 100:
        metric_host_id = f"{alert_key[:70]}:{str(host_id)[-25:]}"
    for metric_time, value in (
        (center - timedelta(minutes=20), max(Decimal("0"), case.value - Decimal("8"))),
        (center - timedelta(minutes=5), max(Decimal("0"), case.value - Decimal("2"))),
        (center, case.value),
    ):
        db.add(
            ObservabilityMetricHistory(
                source_type=case.source_type,
                instance_id=event.instance_id,
                host_id=metric_host_id,
                standard_metric=case.expected_metric,
                metric_value=value,
                metric_unit="%" if case.expected_metric.endswith("used_pct") else "s",
                metric_time=metric_time,
            )
        )
    db.commit()


def _run_case(db, prefix: str, case: Case) -> dict[str, Any]:
    instance = _instance(db, case.host)
    alert_key = f"{prefix}-{case.name}"
    host_id, labels = _labels(db, instance, case)
    payload = {
        "source_type": case.source_type,
        "alert_key": alert_key,
        "host_id": host_id,
        "severity": case.severity,
        "title": case.title,
        "content": case.content,
        "metric_name": case.source_metric,
        "metric_value": str(case.value),
        "status": "firing",
        "labels": labels,
        "annotations": {"summary": case.title, "description": case.content},
        "raw_data": {
            "labels": labels,
            "annotations": {"summary": case.title, "description": case.content},
            "name": case.title,
            "opdata": case.content,
        },
    }
    ingest_started = perf_counter()
    alert_ingest_service.ingest_alert(db, payload)
    ingest_ms = round((perf_counter() - ingest_started) * 1000, 2)
    event = _event_for_case(db, case.source_type, alert_key)
    _insert_metric_history(db, case, event, host_id)

    analysis_started = perf_counter()
    chain = alert_diagnosis_service.build_evidence_chain(db, event, window_minutes=60)
    analysis_ms = round((perf_counter() - analysis_started) * 1000, 2)
    root_cause = chain.get("root_cause") or {}
    event_evidence = (chain.get("evidence") or [{}])[0]

    diag_nodes = diagnosis_evidence_chain._from_alerts(db, instance)
    diag_node = next((item for item in diag_nodes if (item.get("raw") or {}).get("alert_key") == alert_key), None)

    checks = {
        "bound_instance": event.instance_id == instance.id,
        "db_type": event.db_type == instance.db_type,
        "metric": event.metric_name == case.expected_metric,
        "category": root_cause.get("category") == case.expected_category,
        "event_layer": event_evidence.get("layer") == case.expected_event_layer,
        "diagnosis_alert_layer": bool(diag_node and diag_node.get("layer") == case.expected_diag_layer),
        "trend_has_points": (chain.get("trend") or {}).get("point_count", 0) >= 3,
        "analysis_fast": analysis_ms < 1500,
    }
    return {
        "host": case.host,
        "db_type": instance.db_type,
        "source_type": case.source_type,
        "name": case.name,
        "event_id": event.id,
        "expected": {
            "instance_id": instance.id,
            "metric_name": case.expected_metric,
            "category": case.expected_category,
            "event_layer": case.expected_event_layer,
            "diagnosis_alert_layer": case.expected_diag_layer,
        },
        "actual": {
            "instance_id": event.instance_id,
            "db_type": event.db_type,
            "metric_name": event.metric_name,
            "category": root_cause.get("category"),
            "event_layer": event_evidence.get("layer"),
            "diagnosis_alert_layer": diag_node.get("layer") if diag_node else None,
            "trend_points": (chain.get("trend") or {}).get("point_count", 0),
            "summary": chain.get("summary"),
        },
        "timing_ms": {"ingest": ingest_ms, "analysis": analysis_ms},
        "checks": checks,
        "pass": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default=f"codex-live-linkage-{int(time.time())}")
    parser.add_argument("--oracle-host", default=os.getenv("MESGUARD_E2E_ORACLE_HOST"))
    parser.add_argument("--mysql-host", default=os.getenv("MESGUARD_E2E_MYSQL_HOST"))
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    if not args.oracle_host or not args.mysql_host:
        parser.error(
            "--oracle-host and --mysql-host are required, or set "
            "MESGUARD_E2E_ORACLE_HOST and MESGUARD_E2E_MYSQL_HOST"
        )
    hosts = {"oracle": args.oracle_host, "mysql": args.mysql_host}
    cases = [replace(case, host=hosts[case.host]) for case in CASES]

    with SessionLocal() as db:
        db.bind.echo = False
        _cleanup(db, args.prefix)
        try:
            results = [_run_case(db, args.prefix, case) for case in cases]
            report = {
                "prefix": args.prefix,
                "case_count": len(results),
                "passed": sum(1 for item in results if item["pass"]),
                "failed": [item for item in results if not item["pass"]],
                "results": results,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0 if all(item["pass"] for item in results) else 1
        except Exception:
            db.rollback()
            raise
        finally:
            if not args.keep_data:
                _cleanup(db, args.prefix)


if __name__ == "__main__":
    raise SystemExit(main())
