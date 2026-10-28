"""Closed durable-result contract for approval-gated capability effects."""
from __future__ import annotations

import hashlib
import json


EFFECT_RESULT_SCHEMA = "capability.effect_result/v1"
EFFECT_RESULT_VERSION = 3

# These capabilities share one transport envelope, but keep their existing
# domain-specific payloads and evidence validators. Adding an id here is an
# explicit compatibility and queue-publication decision.
COMMON_EFFECT_CAPABILITIES = frozenset({
    "monitor.alert.sync",
    "monitor.capacity.execute",
    "monitor.observability.bind",
    "performance.workload.collect",
    "performance.sql_profile.collect",
    "performance.index.collect",
    "performance.table_profile.collect",
    "performance.baseline.collect",
    "performance.capacity.collect",
    "operations.startup.plan",
    "operations.shutdown.plan",
    "operations.switchover.plan",
    "governance.candidate.scan",
    "protection.backup.plan",
    "protection.restore.plan",
    "integration.zabbix.sync",
    "integration.prometheus.sync",
})

SPECIAL_DURABLE_CONTRACTS = {
    "diagnosis.root_cause.execute": (1, "diagnosis.root_cause_result/v1"),
    "report.document.generate": (3, "report.document_result/v1"),
    "inspection.run.execute": (2, "inspection.plan_result/v1"),
}


def is_common_effect(capability_id: str, version: int | None = None,
                     output_schema: str | None = None) -> bool:
    if capability_id not in COMMON_EFFECT_CAPABILITIES:
        return False
    if version is not None and version != EFFECT_RESULT_VERSION:
        return False
    if output_schema is not None and output_schema != EFFECT_RESULT_SCHEMA:
        return False
    return True


def is_durable_definition(entry: dict) -> bool:
    """Recognize only frozen definition/version/schema combinations we can publish."""
    capability_id = entry.get("capability_id")
    version = entry.get("version", entry.get("definition_version"))
    output_schema = entry.get("output_schema", entry.get("schema"))
    if is_common_effect(capability_id, version, output_schema):
        return True
    return SPECIAL_DURABLE_CONTRACTS.get(capability_id) == (version, output_schema)


def digest(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
