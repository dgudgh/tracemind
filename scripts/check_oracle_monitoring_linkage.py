#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
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
from app.models.external_monitoring import (
    PrometheusAlertCache,
    ZabbixHostMapping,
    ZabbixProblemCache,
)
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
    source_type: str
    name: str
    source_metric: str
    expected_metric: str
    expected_category: str
    expected_layer: str
    value: Decimal
    title: str
    content: str
    severity: str = "warning"


CASES = [
    Case(
        source_type="prometheus",
        name="prom_cpu",
        source_metric="host_cpu_used_pct",
        expected_metric="host.cpu.used_pct",
        expected_category="cpu",
        expected_layer="os",
        value=Decimal("91.5"),
        title="OracleHostCPUHigh",
        content="Oracle database host CPU usage is 91.5 percent.",
    ),
    Case(
        source_type="prometheus",
        name="prom_fra",
        source_metric="oracle_fra_used_pct",
        expected_metric="oracle.fra.used_pct",
        expected_category="oracle_fra",
        expected_layer="database",
        value=Decimal("96.2"),
        title="OracleFRAUsageHigh",
        content="Oracle FRA usage is 96.2 percent and archive cleanup is required.",
        severity="critical",
    ),
    Case(
        source_type="prometheus",
        name="prom_dg",
        source_metric="oracle_dg_lag_seconds",
        expected_metric="oracle.dg.lag_seconds",
        expected_category="replication",
        expected_layer="database",
        value=Decimal("420"),
        title="OracleDataGuardLagHigh",
        content="Oracle Data Guard apply lag is 420 seconds.",
        severity="critical",
    ),
    Case(
        source_type="zabbix",
        name="zbx_memory",
        source_metric="vm.memory.size[pused]",
        expected_metric="host.memory.used_pct",
        expected_category="memory",
        expected_layer="os",
        value=Decimal("88.3"),
        title="Oracle host memory pressure",
        content="Zabbix reports Oracle host memory used percent is 88.3.",
    ),
    Case(
        source_type="zabbix",
        name="zbx_tablespace",
        source_metric="db.tablespace.used_pct",
        expected_metric="db.tablespace.used_pct",
        expected_category="tablespace_capacity",
        expected_layer="database",
        value=Decimal("93.4"),
        title="Oracle tablespace usage high",
        content="Oracle tablespace USERS used percent is 93.4.",
        severity="critical",
    ),
    Case(
        source_type="zabbix",
        name="zbx_fra",
        source_metric="oracle.fra.used_pct",
        expected_metric="oracle.fra.used_pct",
        expected_category="oracle_fra",
        expected_layer="database",
        value=Decimal("95.7"),
        title="Oracle FRA usage high",
        content="Zabbix reports Oracle FRA used percent is 95.7.",
        severity="critical",
    ),
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _first_oracle_instance(db) -> DBInstance:
    row = (
        db.query(DBInstance)
        .filter(DBInstance.enabled == 1, DBInstance.db_type == "oracle")
        .order_by(DBInstance.id.asc())
        .first()
    )
    if not row:
        raise RuntimeError("No enabled Oracle instance found.")
    return row


def _cleanup(db, prefix: str) -> None:
    event_ids = [
        row.id
        for row in db.query(DBAlertEvent)
        .filter(DBAlertEvent.event_uid.like(f"%{prefix}%"))
        .all()
    ]
    if event_ids:
        db.query(DBAlertActionLog).filter(DBAlertActionLog.event_id.in_(event_ids)).delete(synchronize_session=False)
    db.query(DBAlertEvent).filter(DBAlertEvent.event_uid.like(f"%{prefix}%")).delete(synchronize_session=False)
    db.query(ObservabilityAlertEvent).filter(ObservabilityAlertEvent.alert_key.like(f"{prefix}%")).delete(
        synchronize_session=False
    )
    db.query(ObservabilityMetricHistory).filter(ObservabilityMetricHistory.host_id.like(f"{prefix}%")).delete(
        synchronize_session=False
    )
    db.query(ObservabilityInstanceBinding).filter(
        (ObservabilityInstanceBinding.host_identifier.like(f"{prefix}%"))
        | (ObservabilityInstanceBinding.host_ip.like(f"{prefix}%"))
        | (ObservabilityInstanceBinding.zabbix_hostid.like(f"{prefix}%"))
        | (ObservabilityInstanceBinding.prometheus_target.like(f"{prefix}%"))
    ).delete(synchronize_session=False)
    db.query(PrometheusAlertCache).filter(PrometheusAlertCache.instance_name.like(f"{prefix}%")).delete(
        synchronize_session=False
    )
    db.query(ZabbixProblemCache).filter(ZabbixProblemCache.hostid.like(f"{prefix}%")).delete(synchronize_session=False)
    db.query(ZabbixHostMapping).filter(ZabbixHostMapping.zabbix_hostid.like(f"{prefix}%")).delete(
        synchronize_session=False
    )
    db.commit()


def _create_bindings(db, instance: DBInstance, prefix: str) -> dict[str, str]:
    prom_target = f"{prefix}-prometheus:9100"
    zabbix_hostid = f"{prefix}-zabbix"
    db.add_all(
        [
            ObservabilityInstanceBinding(
                instance_id=instance.id,
                host_identifier=f"{prefix}-prometheus",
                host_ip=instance.host,
                prometheus_target=prom_target,
                business_system=instance.system_name or instance.instance_name,
                binding_status="active",
                match_rule="test_prometheus_target",
                confidence=100,
                enabled=1,
                raw_data={"test_prefix": prefix},
            ),
            ObservabilityInstanceBinding(
                instance_id=instance.id,
                host_identifier=f"{prefix}-zabbix-host",
                host_ip=instance.host,
                zabbix_hostid=zabbix_hostid,
                zabbix_host_name=f"{prefix}-zabbix-host",
                business_system=instance.system_name or instance.instance_name,
                binding_status="active",
                match_rule="test_zabbix_host",
                confidence=100,
                enabled=1,
                raw_data={"test_prefix": prefix},
            ),
        ]
    )
    db.commit()
    return {"prometheus": prom_target, "zabbix": zabbix_hostid}


def _host_id(case: Case, hosts: dict[str, str]) -> str:
    return hosts[case.source_type]


def _labels(case: Case, host_id: str) -> dict[str, str]:
    if case.source_type == "prometheus":
        return {
            "alertname": case.title,
            "instance": host_id,
            "job": "oracle-exporter",
            "severity": case.severity,
            "metric": case.source_metric,
        }
    return {
        "hostid": host_id,
        "host": host_id,
        "severity": case.severity,
        "metric": case.source_metric,
    }


def _insert_metric_history(db, case: Case, event: DBAlertEvent, host_id: str) -> None:
    center = event.last_seen or _now()
    points = [
        (center - timedelta(minutes=20), max(Decimal("0"), case.value - Decimal("8"))),
        (center - timedelta(minutes=5), max(Decimal("0"), case.value - Decimal("2"))),
        (center, case.value),
    ]
    for metric_time, value in points:
        db.add(
            ObservabilityMetricHistory(
                source_type=case.source_type,
                instance_id=event.instance_id,
                host_id=host_id,
                standard_metric=case.expected_metric,
                metric_value=value,
                metric_unit="%" if case.expected_metric.endswith("used_pct") else "s",
                metric_time=metric_time,
            )
        )
    db.commit()


def _event_for_case(db, source_type: str, alert_key: str) -> DBAlertEvent:
    event_uid = f"observability:{source_type}:{alert_key}"
    row = db.query(DBAlertEvent).filter(DBAlertEvent.event_uid == event_uid).first()
    if not row:
        raise RuntimeError(f"Unified event not found: {event_uid}")
    return row


def _run_observability_case(db, instance: DBInstance, prefix: str, hosts: dict[str, str], case: Case) -> dict[str, Any]:
    alert_key = f"{prefix}-{case.name}"
    host_id = _host_id(case, hosts)
    labels = _labels(case, host_id)
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
        "annotations": {
            "summary": case.title,
            "description": case.content,
        },
        "raw_data": {
            "labels": labels,
            "annotations": {"summary": case.title, "description": case.content},
            "name": case.title,
            "opdata": case.content,
        },
    }
    ingest_start = perf_counter()
    alert_ingest_service.ingest_alert(db, payload)
    ingest_ms = round((perf_counter() - ingest_start) * 1000, 2)
    event = _event_for_case(db, case.source_type, alert_key)
    _insert_metric_history(db, case, event, host_id)

    analysis_start = perf_counter()
    chain = alert_diagnosis_service.build_evidence_chain(db, event, window_minutes=60)
    analysis_ms = round((perf_counter() - analysis_start) * 1000, 2)
    root_cause = chain.get("root_cause") or {}
    event_evidence = (chain.get("evidence") or [{}])[0]
    checks = {
        "bound_instance": event.instance_id == instance.id,
        "metric": event.metric_name == case.expected_metric,
        "category": root_cause.get("category") == case.expected_category,
        "layer": event_evidence.get("layer") == case.expected_layer,
        "trend_has_points": (chain.get("trend") or {}).get("point_count", 0) >= 3,
        "analysis_fast": analysis_ms < 1000,
    }
    return {
        "name": case.name,
        "source_type": case.source_type,
        "expected": {
            "instance_id": instance.id,
            "metric_name": case.expected_metric,
            "category": case.expected_category,
            "layer": case.expected_layer,
        },
        "actual": {
            "instance_id": event.instance_id,
            "metric_name": event.metric_name,
            "category": root_cause.get("category"),
            "layer": event_evidence.get("layer"),
            "trend_points": (chain.get("trend") or {}).get("point_count", 0),
        },
        "timing_ms": {"ingest": ingest_ms, "analysis": analysis_ms},
        "checks": checks,
        "pass": all(checks.values()),
    }


def _run_legacy_evidence_checks(db, instance: DBInstance, prefix: str) -> list[dict[str, Any]]:
    prom = PrometheusAlertCache(
        alert_name="OracleFRAUsageHigh",
        alert_status="firing",
        severity="critical",
        instance_name=f"{prefix}-legacy-prom",
        db_instance_id=instance.id,
        labels_json={"alertname": "OracleFRAUsageHigh", "metric": "oracle_fra_used_pct"},
        annotations_json={"description": "Oracle FRA usage is 96 percent."},
        starts_at=_now(),
        raw_data={"test_prefix": prefix},
    )
    mapping = ZabbixHostMapping(
        zabbix_config_id=0,
        zabbix_hostid=f"{prefix}-legacy-zabbix",
        zabbix_host_name=f"{prefix}-legacy-zabbix-host",
        visible_name=f"{prefix}-legacy-zabbix-host",
        ip_address=instance.host,
        db_instance_id=instance.id,
        enabled=1,
    )
    zbx = ZabbixProblemCache(
        zabbix_config_id=0,
        eventid=f"{prefix}-legacy-zbx-event",
        objectid="123456",
        hostid=f"{prefix}-legacy-zabbix",
        host_name=f"{prefix}-legacy-zabbix-host",
        problem_name="Oracle tablespace USERS usage high",
        severity=4,
        severity_name="High",
        status="firing",
        raw_data={"item_key": "db.tablespace.used_pct", "test_prefix": prefix},
    )
    db.add_all([prom, mapping, zbx])
    db.commit()

    nodes = diagnosis_evidence_chain._from_alerts(db, instance)
    prom_node = next((item for item in nodes if item.get("source") == "prometheus" and prefix in str(item)), None)
    zbx_node = next((item for item in nodes if item.get("source") == "zabbix" and prefix in str(item)), None)
    checks = []
    checks.append(
        {
            "name": "legacy_prometheus_fra_layer",
            "expected": {"layer": C.LAYER_DB, "metric_name": "oracle.fra.used_pct"},
            "actual": {
                "layer": prom_node.get("layer") if prom_node else None,
                "metric_name": prom_node.get("metric_name") if prom_node else None,
            },
            "pass": bool(
                prom_node
                and prom_node.get("layer") == C.LAYER_DB
                and prom_node.get("metric_name") == "oracle.fra.used_pct"
            ),
        }
    )
    checks.append(
        {
            "name": "legacy_zabbix_tablespace_layer",
            "expected": {"layer": C.LAYER_DB, "metric_name": "db.tablespace.used_pct"},
            "actual": {
                "layer": zbx_node.get("layer") if zbx_node else None,
                "metric_name": zbx_node.get("metric_name") if zbx_node else None,
            },
            "pass": bool(
                zbx_node
                and zbx_node.get("layer") == C.LAYER_DB
                and zbx_node.get("metric_name") == "db.tablespace.used_pct"
            ),
        }
    )
    return checks


def _run_observability_evidence_checks(db, prefix: str) -> list[dict[str, Any]]:
    nodes = diagnosis_evidence_chain._from_alerts(db, _first_oracle_instance(db))

    def by_alert_key(source: str, alert_key: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in nodes
                if item.get("source") == source
                and (item.get("raw") or {}).get("alert_key") == alert_key
            ),
            None,
        )

    prom_node = by_alert_key("prometheus", f"{prefix}-prom_fra")
    zbx_node = by_alert_key("zabbix", f"{prefix}-zbx_memory")
    return [
        {
            "name": "observability_prometheus_fra_in_diagnosis_alerts",
            "expected": {"layer": C.LAYER_DB, "metric_name": "oracle.fra.used_pct"},
            "actual": {
                "layer": prom_node.get("layer") if prom_node else None,
                "metric_name": prom_node.get("metric_name") if prom_node else None,
            },
            "pass": bool(
                prom_node
                and prom_node.get("layer") == C.LAYER_DB
                and prom_node.get("metric_name") == "oracle.fra.used_pct"
            ),
        },
        {
            "name": "observability_zabbix_memory_in_diagnosis_alerts",
            "expected": {"layer": C.LAYER_OS, "metric_name": "host.memory.used_pct"},
            "actual": {
                "layer": zbx_node.get("layer") if zbx_node else None,
                "metric_name": zbx_node.get("metric_name") if zbx_node else None,
            },
            "pass": bool(
                zbx_node
                and zbx_node.get("layer") == C.LAYER_OS
                and zbx_node.get("metric_name") == "host.memory.used_pct"
            ),
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default=f"codex-oracle-linkage-{int(time.time())}")
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        _cleanup(db, args.prefix)
        instance = _first_oracle_instance(db)
        hosts = _create_bindings(db, instance, args.prefix)
        try:
            observability_results = [
                _run_observability_case(db, instance, args.prefix, hosts, case)
                for case in CASES
            ]
            observability_evidence_results = _run_observability_evidence_checks(db, args.prefix)
            legacy_results = _run_legacy_evidence_checks(db, instance, args.prefix)
            results = observability_results + observability_evidence_results + legacy_results
            report = {
                "oracle_instance": {
                    "id": instance.id,
                    "name": instance.instance_name,
                    "host": instance.host,
                },
                "case_count": len(results),
                "passed": sum(1 for item in results if item["pass"]),
                "failed": [item for item in results if not item["pass"]],
                "results": results,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0 if all(item["pass"] for item in results) else 1
        finally:
            if not args.keep_data:
                _cleanup(db, args.prefix)


if __name__ == "__main__":
    raise SystemExit(main())
