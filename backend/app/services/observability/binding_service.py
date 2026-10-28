from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.http_security import require_http_url
from app.models.db_instance import DBInstance
from app.models.external_monitoring import ZabbixHostMapping
from app.models.observability import ObservabilityInstanceBinding, ObservabilitySource


def binding_to_dict(row: ObservabilityInstanceBinding) -> dict[str, Any]:
    return {
        "id": row.id,
        "instance_id": row.instance_id,
        "host_identifier": row.host_identifier,
        "host_ip": row.host_ip,
        "zabbix_source_id": row.zabbix_source_id,
        "zabbix_hostid": row.zabbix_hostid,
        "zabbix_host_name": row.zabbix_host_name,
        "prometheus_source_id": row.prometheus_source_id,
        "prometheus_target": row.prometheus_target,
        "business_system": row.business_system,
        "binding_status": row.binding_status,
        "match_rule": row.match_rule,
        "confidence": row.confidence,
        "enabled": row.enabled,
        "raw_data": row.raw_data,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def list_bindings(db: Session, instance_id: int | None = None, enabled: int | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilityInstanceBinding)
    if instance_id:
        query = query.filter(ObservabilityInstanceBinding.instance_id == instance_id)
    if enabled is not None:
        query = query.filter(ObservabilityInstanceBinding.enabled == enabled)
    return [binding_to_dict(row) for row in query.order_by(ObservabilityInstanceBinding.id.desc()).all()]


def upsert_binding(db: Session, data: dict[str, Any], binding: ObservabilityInstanceBinding | None = None) -> dict[str, Any]:
    row = binding or _find_existing_binding(db, data)
    if not row:
        row = ObservabilityInstanceBinding(instance_id=int(data["instance_id"]))
        db.add(row)
    for field in (
        "instance_id",
        "host_identifier",
        "host_ip",
        "zabbix_source_id",
        "zabbix_hostid",
        "zabbix_host_name",
        "prometheus_source_id",
        "prometheus_target",
        "business_system",
        "binding_status",
        "match_rule",
        "confidence",
        "enabled",
        "raw_data",
    ):
        if field in data:
            setattr(row, field, data[field])
    row.binding_status = row.binding_status or "active"
    row.enabled = 1 if row.enabled is None else row.enabled
    row.confidence = row.confidence or 80
    db.commit()
    db.refresh(row)
    return binding_to_dict(row)


def delete_binding(db: Session, row: ObservabilityInstanceBinding) -> None:
    db.delete(row)
    db.commit()


def get_binding(db: Session, binding_id: int) -> ObservabilityInstanceBinding | None:
    return db.query(ObservabilityInstanceBinding).filter(ObservabilityInstanceBinding.id == binding_id).first()


def auto_bind(db: Session) -> dict[str, Any]:
    instances = db.query(DBInstance).filter(DBInstance.enabled == 1).all()
    zabbix_sources = db.query(ObservabilitySource).filter(ObservabilitySource.source_type == "zabbix", ObservabilitySource.enabled == 1).all()
    prometheus_sources = db.query(ObservabilitySource).filter(ObservabilitySource.source_type == "prometheus", ObservabilitySource.enabled == 1).all()
    zabbix_hosts = _load_zabbix_hosts(db, zabbix_sources)
    prometheus_targets = _load_prometheus_targets(prometheus_sources)

    created_or_updated = 0
    binding_ids: list[int] = []
    for instance in instances:
        candidates = _instance_candidates(instance)
        zbx = _match_zabbix_host(candidates, zabbix_hosts)
        prom = _match_prometheus_target(candidates, prometheus_targets)
        old_zbx = _match_legacy_zabbix(db, instance, candidates)
        if old_zbx and not zbx:
            zbx = old_zbx
        if not zbx and not prom and not candidates:
            continue
        saved = upsert_binding(
            db,
            {
                "instance_id": instance.id,
                "host_identifier": _first(candidates),
                "host_ip": _first_ip(candidates),
                "zabbix_source_id": zbx.get("source_id") if zbx else None,
                "zabbix_hostid": zbx.get("hostid") if zbx else None,
                "zabbix_host_name": zbx.get("name") or zbx.get("host") if zbx else None,
                "prometheus_source_id": prom.get("source_id") if prom else None,
                "prometheus_target": prom.get("target") if prom else None,
                "business_system": instance.system_name,
                "binding_status": "active",
                "match_rule": _match_rule(zbx, prom),
                "confidence": _confidence(zbx, prom),
                "enabled": 1,
                "raw_data": {"zabbix": zbx, "prometheus": prom, "instance_host": instance.host},
            },
        )
        binding_ids.append(int(saved["id"]))
        created_or_updated += 1
    return {
        "status": "success",
        "matched_count": created_or_updated,
        "binding_ids": sorted(binding_ids),
        "zabbix_host_count": len(zabbix_hosts),
        "prometheus_target_count": len(prometheus_targets),
    }


def resolve_instance(
    db: Session,
    *,
    source_type: str | None = None,
    host_id: str | None = None,
    labels: dict[str, Any] | None = None,
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direct_instance_id = _int((raw_data or {}).get("instance_id") or (labels or {}).get("instance_id"))
    if direct_instance_id:
        instance = db.query(DBInstance).filter(DBInstance.id == direct_instance_id).first()
        if instance:
            return _resolved(instance, None, "payload.instance_id")

    candidates = _candidate_strings(host_id, labels, raw_data)
    if not candidates:
        return {}

    binding = _find_binding_by_candidates(db, source_type, candidates)
    if binding:
        instance = db.query(DBInstance).filter(DBInstance.id == binding.instance_id).first()
        if instance:
            return _resolved(instance, binding, "observability_binding")

    legacy = _find_legacy_binding_by_candidates(db, candidates)
    if legacy and legacy.db_instance_id:
        instance = db.query(DBInstance).filter(DBInstance.id == legacy.db_instance_id).first()
        if instance:
            return _resolved(instance, None, "legacy_zabbix_mapping")

    instance = _find_instance_by_candidates(db, candidates)
    if instance:
        return _resolved(instance, None, "instance_host")
    return {}


def _resolved(instance: DBInstance, binding: ObservabilityInstanceBinding | None, match_rule: str) -> dict[str, Any]:
    return {
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "instance_name": instance.instance_name,
        "business_system": binding.business_system if binding else instance.system_name,
        "binding_id": binding.id if binding else None,
        "match_rule": match_rule,
    }


def _find_existing_binding(db: Session, data: dict[str, Any]) -> ObservabilityInstanceBinding | None:
    filters = []
    if data.get("zabbix_hostid"):
        filters.append(ObservabilityInstanceBinding.zabbix_hostid == str(data["zabbix_hostid"]))
    if data.get("prometheus_target"):
        filters.append(ObservabilityInstanceBinding.prometheus_target == str(data["prometheus_target"]))
    if data.get("instance_id") and data.get("host_ip"):
        filters.append(
            (ObservabilityInstanceBinding.instance_id == int(data["instance_id"]))
            & (ObservabilityInstanceBinding.host_ip == str(data["host_ip"]))
        )
    if not filters:
        return None
    return db.query(ObservabilityInstanceBinding).filter(or_(*filters)).first()


def _load_zabbix_hosts(db: Session, sources: list[ObservabilitySource]) -> list[dict[str, Any]]:
    from app.services.observability import zabbix_collector

    result: list[dict[str, Any]] = []
    for source in sources:
        try:
            hosts = zabbix_collector.api_call(
                source,
                "host.get",
                {
                    "output": ["hostid", "host", "name", "status"],
                    "selectInterfaces": ["ip", "dns", "port", "type", "main"],
                },
            )
        except Exception:
            hosts = []
        for host in hosts or []:
            host["source_id"] = source.id
            result.append(host)
    return result


def _load_prometheus_targets(sources: list[ObservabilitySource]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for source in sources:
        if not source.api_url:
            continue
        try:
            url = require_http_url(f"{source.api_url.rstrip('/')}/api/v1/targets?state=active")
            req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=source.timeout_seconds or 10) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception:
            continue
        for item in ((body.get("data") or {}).get("activeTargets") or []):
            labels = item.get("labels") or {}
            discovered = item.get("discoveredLabels") or {}
            scrape_address = _address_from_url(item.get("scrapeUrl"))
            discovered_address = discovered.get("__address__")
            label_instance = labels.get("instance")
            label_host = labels.get("host")
            target = discovered_address or scrape_address or label_instance or label_host or item.get("scrapeUrl")
            aliases = _ordered_aliases(
                target,
                label_instance,
                label_host,
                labels.get("job"),
                discovered_address,
                scrape_address,
                item.get("scrapeUrl"),
            )
            if target:
                targets.append(
                    {
                        "source_id": source.id,
                        "target": target,
                        "aliases": aliases,
                        "labels": labels,
                        "raw": item,
                    }
                )
    return targets


def _match_zabbix_host(candidates: set[str], hosts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for host in hosts:
        values = {str(host.get("host") or "").lower(), str(host.get("name") or "").lower()}
        for iface in host.get("interfaces") or []:
            values.add(str(iface.get("ip") or "").lower())
            values.add(str(iface.get("dns") or "").lower())
        if values & candidates:
            return host
    return None


def _match_prometheus_target(candidates: set[str], targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for target in targets:
        labels = target.get("labels") or {}
        values = _normalize_candidates(
            [
                target.get("target"),
                *(target.get("aliases") or []),
                *(labels.values() if isinstance(labels, dict) else []),
            ]
        )
        if values & candidates:
            return target
    return None


def _match_legacy_zabbix(db: Session, instance: DBInstance, candidates: set[str]) -> dict[str, Any] | None:
    rows = db.query(ZabbixHostMapping).filter(ZabbixHostMapping.enabled == 1).all()
    for row in rows:
        values = {
            str(row.zabbix_hostid or "").lower(),
            str(row.zabbix_host_name or "").lower(),
            str(row.visible_name or "").lower(),
            str(row.ip_address or "").lower(),
        }
        if row.db_instance_id == instance.id or values & candidates:
            return {
                "source_id": row.zabbix_config_id,
                "hostid": row.zabbix_hostid,
                "host": row.zabbix_host_name,
                "name": row.visible_name or row.zabbix_host_name,
            }
    return None


def _find_binding_by_candidates(db: Session, source_type: str | None, candidates: set[str]) -> ObservabilityInstanceBinding | None:
    query = db.query(ObservabilityInstanceBinding).filter(ObservabilityInstanceBinding.enabled == 1)
    if source_type == "zabbix":
        query = query.filter(ObservabilityInstanceBinding.zabbix_hostid.is_not(None))
    if source_type in {"prometheus", "alertmanager"}:
        query = query.filter(ObservabilityInstanceBinding.prometheus_target.is_not(None))
    matches: list[tuple[int, int, int, ObservabilityInstanceBinding]] = []
    for row in query.all():
        score = _binding_match_score(row, source_type, candidates)
        if score:
            matches.append((score, row.confidence or 0, row.id or 0, row))
    if matches:
        return sorted(matches, key=lambda item: (item[0], item[1], item[2]), reverse=True)[0][3]
    return None


def _binding_match_score(row: ObservabilityInstanceBinding, source_type: str | None, candidates: set[str]) -> int:
    if source_type in {"prometheus", "alertmanager"}:
        if _exact_values(row.prometheus_target) & candidates:
            return 100
    if source_type == "zabbix":
        if _exact_values(row.zabbix_host_name, row.host_identifier) & candidates:
            return 100
        if _exact_values(row.zabbix_hostid) & candidates:
            return 90
    if _exact_values(row.host_identifier, row.business_system) & candidates:
        return 70
    if _exact_values(row.host_ip) & candidates:
        return 60
    if _binding_candidates(row) & candidates:
        return 50
    return 0


def _find_legacy_binding_by_candidates(db: Session, candidates: set[str]) -> ZabbixHostMapping | None:
    for row in db.query(ZabbixHostMapping).filter(ZabbixHostMapping.enabled == 1).all():
        values = {
            str(row.zabbix_hostid or "").lower(),
            str(row.zabbix_host_name or "").lower(),
            str(row.visible_name or "").lower(),
            str(row.ip_address or "").lower(),
        }
        if values & candidates:
            return row
    return None


def _find_instance_by_candidates(db: Session, candidates: set[str]) -> DBInstance | None:
    for instance in db.query(DBInstance).filter(DBInstance.enabled == 1).all():
        if _instance_candidates(instance) & candidates:
            return instance
    return None


def _instance_candidates(instance: DBInstance) -> set[str]:
    return _normalize_candidates([instance.host, instance.ssh_host, instance.instance_name, instance.system_name])


def _candidate_strings(host_id: str | None, labels: dict[str, Any] | None, raw_data: dict[str, Any] | None) -> set[str]:
    values = [host_id]
    for source in (labels or {}, raw_data or {}):
        for key in (
            "instance",
            "host",
            "hostname",
            "host_name",
            "hostid",
            "zabbix_hostid",
            "ip",
            "target",
            "job",
        ):
            values.append(source.get(key))
    return _normalize_candidates(values)


def _normalize_candidates(values: list[Any]) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        result.add(text)
        result.add(_strip_port(text))
        parsed = urllib.parse.urlparse(text if "://" in text else f"//{text}")
        if parsed.hostname:
            result.add(parsed.hostname.lower())
    return {item for item in result if item}


def _exact_values(*values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        result.add(text)
        parsed = urllib.parse.urlparse(text if "://" in text else f"//{text}")
        if parsed.netloc:
            result.add(parsed.netloc.lower())
    return result


def _binding_candidates(row: ObservabilityInstanceBinding) -> set[str]:
    values: list[Any] = [
        row.host_identifier,
        row.host_ip,
        row.zabbix_hostid,
        row.zabbix_host_name,
        row.prometheus_target,
        row.business_system,
    ]
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    for key in ("zabbix", "prometheus"):
        section = raw.get(key)
        if not isinstance(section, dict):
            continue
        values.extend(
            [
                section.get("hostid"),
                section.get("host"),
                section.get("name"),
                section.get("target"),
            ]
        )
        aliases = section.get("aliases")
        if isinstance(aliases, list):
            values.extend(aliases)
        labels = section.get("labels")
        if isinstance(labels, dict):
            values.extend(labels.values())
    return _normalize_candidates(values)


def _ordered_aliases(*values: Any) -> list[str]:
    aliases: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text and text not in aliases:
            aliases.append(text)
        stripped = _strip_port(text.lower())
        if stripped and stripped not in aliases:
            aliases.append(stripped)
    return aliases


def _address_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    return parsed.netloc or None


def _strip_port(value: str) -> str:
    if not value:
        return value
    parsed = urllib.parse.urlparse(value if "://" in value else f"//{value}")
    if parsed.hostname:
        return parsed.hostname.lower()
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _first(values: set[str]) -> str | None:
    return sorted(values)[0] if values else None


def _first_ip(values: set[str]) -> str | None:
    for value in sorted(values):
        parts = value.split(".")
        if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            return value
    return None


def _match_rule(zbx: dict[str, Any] | None, prom: dict[str, Any] | None) -> str:
    if zbx and prom:
        return "zabbix_and_prometheus"
    if zbx:
        return "zabbix_host"
    if prom:
        return "prometheus_target"
    return "instance_host"


def _confidence(zbx: dict[str, Any] | None, prom: dict[str, Any] | None) -> int:
    if zbx and prom:
        return 95
    if zbx or prom:
        return 85
    return 70


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
