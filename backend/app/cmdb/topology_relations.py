"""Relations backed by endpoint observations or explicit saved relationships."""

from copy import deepcopy
from dataclasses import replace

from app.cmdb.discovery import _relation_external_id
from app.cmdb.replication import manual_relation, normalized_role_derived, resolve_upstream, role_derived, upstream_evidence

PROJECTION_METHOD = "cmdb_relation_projection"


def _managed_projection(row):
    return isinstance(row.attributes, dict) and row.attributes.get("discovery_method") == PROJECTION_METHOD


def snapshot_normalized_relations(db):
    from app.cmdb.models import AssetRelation

    fields = ("source_type", "source_id", "target_type", "target_id", "relation_type",
              "status", "discovery_source", "attributes")
    return {
        row.relation_key: {field: deepcopy(getattr(row, field)) for field in fields}
        for row in db.query(AssetRelation).filter(AssetRelation.relation_type == "replicates_to").all()
        if not normalized_role_derived(row)
        and not _managed_projection(row)
    }


def reconcile_normalized_relations(db, inventory, saved):
    """Reconcile inside the caller's transaction, before any projection is published."""
    from app.cmdb.models import AssetInstance, AssetRelation, CmdbRelation
    from app.cmdb.normalized_service import _instance_stable_key

    rows = {row.relation_key: row for row in db.query(AssetRelation).filter(
        AssetRelation.relation_type == "replicates_to",
    ).all()}
    for key, row in rows.items():
        if key in saved:
            for field, value in saved[key].items():
                setattr(row, field, value)
        elif normalized_role_derived(row) or _managed_projection(row):
            row.status = "inactive"

    assets = {asset["id"]: asset for asset in inventory if asset.get("asset_type") == "database_instance"}
    instances = {row.stable_key: row for row in db.query(AssetInstance).all()}
    relations = db.query(CmdbRelation).filter(CmdbRelation.relation_type == "replicates_to").order_by(CmdbRelation.id).all()
    valid_ids = {edge["relation_id"] for edge in saved_topology_edges(list(assets.values()), relations)}
    for relation in relations:
        if relation.id not in valid_ids:
            continue
        source = instances.get(_instance_stable_key(assets[relation.source_asset_id]))
        target = instances.get(_instance_stable_key(assets[relation.target_asset_id]))
        if (source is None or target is None or source.id == target.id
                or source.cluster_id != target.cluster_id
                or source.tenant_id != target.tenant_id or source.organization_id != target.organization_id):
            continue
        key = f"cmdb-relation:{relation.id}:replicates_to"
        row = rows.get(key)
        if row is None:
            row = AssetRelation(relation_key=key, relation_type="replicates_to")
            db.add(row)
            rows[key] = row
        row.source_type, row.source_id = "instance", source.id
        row.target_type, row.target_id = "instance", target.id
        row.status = "active"
        row.discovery_source = "automatic"
        row.attributes = {"discovery_method": PROJECTION_METHOD,
                          "provenance": "manual" if manual_relation(relation.raw_payload) else "observed",
                          "cmdb_relation_id": relation.id, "source_id": relation.source_id,
                          "evidence": deepcopy(relation.raw_payload)}
    db.flush()


def topology_scope(asset):
    attrs = asset.get("attributes") or {}
    group = attrs.get("topology_group") or f"standalone:{asset.get('id', asset.get('external_id'))}"
    if attrs.get("architecture_type") in {"single", "standalone"}:
        group = f"standalone:{asset.get('id', asset.get('external_id'))}"
    return (asset.get("source_id"), str(attrs.get("db_type") or "").lower(),
            attrs.get("tenant_id"), attrs.get("organization_id"), group)


def verified_pairs(assets):
    databases = [asset for asset in assets if asset.get("asset_type") == "database_instance"
                 and asset.get("status", "active") == "active"]
    pairs = []
    for target in databases:
        peers = [asset for asset in databases if topology_scope(asset) == topology_scope(target)]
        source, evidence = resolve_upstream(target, peers)
        if source is not None:
            pairs.append((source, target, evidence))
    return pairs


def prepare_discovery_relations(bundle, detected_profiles=None):
    """Replace legacy role products before preview, persistence or projection."""
    databases = [asset for asset in bundle.assets if asset.get("asset_type") == "database_instance"]
    for target in databases:
        attrs = target.setdefault("attributes", {})
        profile = (detected_profiles or {}).get(attrs.get("native_instance_id"))
        if isinstance(profile, dict):
            # The discovery serializer omits members, including PG receiver rows.
            attrs["replication"] = upstream_evidence(profile)
        elif attrs.get("native_type") == "discovered_database_instance":
            peer = attrs.get("architecture_evidence")
            peer = peer if isinstance(peer, dict) else {}
            evidence = peer.get("replication")
            if peer.get("direct_probe") is True and isinstance(evidence, dict):
                attrs["replication"] = deepcopy(evidence)
            else:
                attrs["replication"] = {"state": "unknown", "reason": "peer_upstream_not_collected"}
        peers = [asset for asset in databases if topology_scope(asset) == topology_scope(target)]
        _, evidence = resolve_upstream(target, peers)
        attrs["replication"] = evidence
    relations = [item for item in bundle.relations
                 if item["relation_type"] != "replicates_to" or not role_derived(item.get("raw_payload"))]
    keys = {item["external_id"] for item in relations}
    for source, target, evidence in verified_pairs(databases):
        external_id = _relation_external_id("replicates_to", source["external_id"], target["external_id"])
        if external_id in keys:
            continue
        relations.append({
            "external_id": external_id,
            "source_external_id": source["external_id"], "target_external_id": target["external_id"],
            "relation_type": "replicates_to",
            "raw_payload": {"discovery_method": "upstream_metadata", "provenance": "observed", **evidence},
        })
        keys.add(external_id)
    return replace(bundle, relations=relations)


def saved_topology_edges(members, relations):
    """Never infer a star or a direction from role labels, including RAC/MGR."""
    by_id = {asset["id"]: asset for asset in members
             if asset.get("asset_type") == "database_instance" and asset.get("status") == "active"}
    edges = []
    seen = set()
    for row in relations:
        if row.status != "active" or row.relation_type != "replicates_to" or role_derived(row.raw_payload):
            continue
        source, target = by_id.get(row.source_asset_id), by_id.get(row.target_asset_id)
        if source is None or target is None or source["id"] == target["id"]:
            continue
        if (source["source_id"] != row.source_id or target["source_id"] != row.source_id
                or topology_scope(source) != topology_scope(target)):
            continue
        if any((asset.get("attributes") or {}).get("architecture_type") in {"rac", "oracle_rac", "single", "standalone"}
               for asset in (source, target)):
            continue
        manual = manual_relation(row.raw_payload)
        if not manual and any((asset.get("attributes") or {}).get("discovery_status") == "failed"
                              for asset in (source, target)):
            continue
        pair = (source["id"], target["id"])
        if pair in seen:
            continue
        seen.add(pair)
        edges.append({
            "id": f"relation:{row.id}", "source": str(source["id"]), "target": str(target["id"]),
            "relation_type": "replicates_to", "label": "复制",
            "provenance": "manual" if manual else "saved_relation", "relation_id": row.id,
        })
    return edges
