from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import socket

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.cmdb import discovery, service
from app.cmdb.models import AssetInstance, AssetRelation, CmdbAsset, CmdbRelation
from app.cmdb.replication import resolve_upstream
from app.cmdb.topology_relations import prepare_discovery_relations, saved_topology_edges, verified_pairs
from app.models.db_instance import DBInstance
from test_asset_center import MODELS, add_mysql_pair


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Topology regression tests must remain offline")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in MODELS:
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    add_mysql_pair(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def asset(number, *, upstream=None, **attrs):
    attributes = {
        "db_type": "mysql", "host": f"198.51.100.{number}", "port": 3306,
        "architecture_type": "replication", "topology_group": "test",
        "role_type": "primary" if number == 1 else "replica",
        **attrs,
    }
    if upstream is not None:
        attributes["replication"] = {"state": "verified", "upstream": upstream}
    return {"id": number, "external_id": f"db-instance:{number}", "source_id": 1,
            "asset_type": "database_instance", "status": "active", "attributes": attributes}


def relation(number, source, target, **kwargs):
    return SimpleNamespace(id=number, source_id=1, source_asset_id=source, target_asset_id=target,
                           relation_type="replicates_to", status=kwargs.get("status", "active"),
                           raw_payload=kwargs.get("payload", {"provenance": "manual"}))


def test_roles_only_discovery_does_not_persist_replication(db):
    preview = service.preview_internal_discovery(db)
    assert preview["relation_count"] == 4
    result = service.apply_internal_discovery(db)
    assert result["applied_relation_count"] == 4
    assert db.query(CmdbRelation).filter_by(relation_type="replicates_to", status="active").count() == 0
    assert service.topology_groups(db)["groups"][0]["replication_state"] == "unknown"
    assert db.query(AssetRelation).filter_by(relation_type="replicates_to", status="active").count() == 0


def test_verified_upstream_creates_idempotent_cmdb_edge_with_evidence(db):
    profiles = {
        1: {"architecture_type": "replication", "role_type": "primary", "topology_group": "verified"},
        2: {"architecture_type": "replication", "role_type": "replica", "topology_group": "verified",
            "source_host": "198.51.100.152", "source_port": 3384},
    }
    for _ in range(2):
        bundle = discovery.build_internal_discovery(db, detected_profiles=profiles)
        with patch.object(service, "build_internal_discovery", return_value=bundle):
            service.apply_internal_discovery(db)
    rows = db.query(CmdbRelation).filter_by(relation_type="replicates_to", status="active").all()
    assert len(rows) == 1
    assert rows[0].raw_payload["state"] == "verified"
    assert rows[0].raw_payload["upstream"] == {"host": "198.51.100.152", "port": 3384}
    group = service.topology_groups(db)["groups"][0]
    assert len(group["edges"]) == 1
    assert group["edges"][0]["relation_id"] == rows[0].id
    assert group["replication_state"] == "verified"
    assert service.topology_groups(db, allowed_native_instance_ids={2})["groups"][0]["edges"] == []
    normalized = db.query(AssetRelation).filter_by(relation_type="replicates_to", status="active").all()
    assert len(normalized) == 1
    assert normalized[0].attributes["cmdb_relation_id"] == rows[0].id


def test_verified_cascade_is_not_replaced_with_primary_star():
    nodes = [asset(1), asset(2, upstream={"host": "198.51.100.1", "port": 3306}),
             asset(3, upstream={"host": "198.51.100.2", "port": 3306})]
    assert [(s["id"], t["id"]) for s, t, _ in verified_pairs(nodes)] == [(1, 2), (2, 3)]
    edges = saved_topology_edges(nodes, [relation(1, 1, 2), relation(2, 2, 3)])
    assert [(edge["source"], edge["target"]) for edge in edges] == [("1", "2"), ("2", "3")]


@pytest.mark.parametrize("upstream", [
    {}, {"host": "198.51.100.1"}, {"port": 3306}, {"host": "198.51.100.1", "port": "bad"},
    {"host": "198.51.100.1", "port": 3307}, {"host": "198.51.100.1", "port": -1},
    {"host": "198.51.100.1", "port": 3306, "server_uuid": "conflicting-id"},
    {"host": "198.51.100.2", "port": 3306},
])
def test_incomplete_conflicting_or_self_endpoints_have_no_edge(upstream):
    nodes = [asset(1), asset(2, upstream=upstream)]
    assert verified_pairs(nodes) == []


@pytest.mark.parametrize("change", [
    {"source_id": 2}, {"asset_type": "host"}, {"status": "inactive"},
    {"attributes": {"tenant_id": 2}}, {"attributes": {"organization_id": 2}},
    {"attributes": {"db_type": "postgresql"}}, {"attributes": {"topology_group": "other"}},
])
def test_endpoint_resolution_stays_inside_asset_scope(change):
    source = asset(1)
    if "attributes" in change:
        source["attributes"].update(change["attributes"])
    else:
        source.update(change)
    assert verified_pairs([source, asset(2, upstream={"host": "198.51.100.1", "port": 3306})]) == []


def test_duplicate_endpoint_is_ambiguous_and_duplicate_inventory_is_idempotent():
    a = asset(1)
    b = asset(2, upstream={"host": "198.51.100.1", "port": 3306})
    ambiguous = asset(3, host="198.51.100.1")
    assert verified_pairs([a, b, ambiguous]) == []
    assert resolve_upstream(b, [a, a, b])[0] is a


def test_failed_probe_does_not_reuse_previous_verified_endpoint():
    source = asset(1)
    failed = asset(2, upstream={"host": "198.51.100.1", "port": 3306}, discovery_status="failed")
    assert resolve_upstream(failed, [source, failed]) == (None, {"state": "unknown", "reason": "probe_failed"})
    observed = relation(1, 1, 2, payload={"provenance": "observed"})
    assert saved_topology_edges([source, failed], [observed]) == []
    assert len(saved_topology_edges([source, failed], [relation(2, 1, 2)])) == 1


def test_saved_edges_filter_status_identity_type_scope_and_legacy_derivation():
    nodes = [asset(1), asset(2), asset(3)]
    rows = [relation(1, 1, 2), relation(2, 1, 2), relation(3, 2, 3),
            relation(4, 1, 3, status="inactive"), relation(5, 1, 999), relation(6, 1, 1),
            relation(7, 1, 3, payload={"discovery_method": "database_role"}),
            relation(8, 1, 3, payload={"provenance": "derived"})]
    wrong_source = relation(9, 1, 3)
    wrong_source.source_id = 2
    rows.append(wrong_source)
    assert [(e["source"], e["target"]) for e in saved_topology_edges(nodes, rows)] == [("1", "2"), ("2", "3")]
    assert [(e["source"], e["target"]) for e in saved_topology_edges(nodes[1:], rows)] == [("2", "3")]


@pytest.mark.parametrize("architecture", ["rac", "oracle_rac", "single", "standalone"])
def test_shared_database_architectures_do_not_fabricate_replication(architecture):
    nodes = [asset(1, architecture_type=architecture), asset(2, architecture_type=architecture)]
    assert saved_topology_edges(nodes, [relation(1, 1, 2)]) == []


def test_manual_relations_survive_sync_and_legacy_role_edges_retire(db):
    service.apply_internal_discovery(db)
    assets = db.query(CmdbAsset).filter_by(asset_type="database_instance").order_by(CmdbAsset.id).all()
    source_id = assets[0].source_id
    for name, payload, status in [
        ("manual-active", {"provenance": "manual"}, "active"),
        ("manual-inactive", {"provenance": "manual"}, "inactive"),
        ("legacy", {"discovery_method": "database_role"}, "active"),
    ]:
        db.add(CmdbRelation(source_id=source_id, external_id=name,
                            source_asset_id=assets[0].id, target_asset_id=assets[1].id,
                            relation_type="replicates_to", status=status, raw_payload=payload))
    db.commit()
    service.apply_internal_discovery(db)
    assert db.query(CmdbRelation).filter_by(external_id="manual-active").one().status == "active"
    assert db.query(CmdbRelation).filter_by(external_id="manual-inactive").one().status == "inactive"
    assert db.query(CmdbRelation).filter_by(external_id="legacy").one().status == "pending_confirmation"
    assert len(service.topology_groups(db)["groups"][0]["edges"]) == 1


def test_matching_group_names_from_different_sources_are_not_combined(db):
    service.apply_internal_discovery(db)
    inventory = service.list_inventory(db)
    clone = deepcopy(next(a for a in inventory if a["asset_type"] == "database_instance"))
    clone.update(id=9999, source_id=9999, name="different source")
    with patch.object(service, "list_inventory", return_value=inventory + [clone]):
        topology = service.topology_groups(db)
    assert topology["group_count"] == 2
    assert len({group["id"] for group in topology["groups"]}) == 2
    assert all(not group["edges"] for group in topology["groups"])


def test_pg_receiver_direction_resolves_without_role_inference():
    nodes = [asset(1, db_type="postgresql"), asset(2, db_type="postgresql",
             architecture_verified=True, architecture_evidence={
                 "in_recovery": True, "members": [{"slot_name": "slot1", "host": "198.51.100.1", "port": 3306}],
             })]
    assert [(s["id"], t["id"]) for s, t, _ in verified_pairs(nodes)] == [(1, 2)]


def test_pg_receiver_evidence_is_preserved_before_discovery_drops_members(db):
    for instance in db.query(DBInstance).all():
        instance.db_type = "postgresql"
        instance.port = 5432
        instance.architecture_type = "streaming_replication"
    db.commit()
    profiles = {
        1: {"architecture_type": "streaming_replication", "role_type": "primary", "topology_group": "pg-test"},
        2: {"architecture_type": "streaming_replication", "role_type": "replica", "topology_group": "pg-test",
            "in_recovery": True, "members": [{"slot_name": "slot1", "host": "198.51.100.152", "port": 5432}]},
    }
    bundle = discovery.build_internal_discovery(db, detected_profiles=profiles)
    prepared = prepare_discovery_relations(bundle, profiles)
    rows = [r for r in prepared.relations if r["relation_type"] == "replicates_to"]
    assert len(rows) == 1
    assert rows[0]["raw_payload"]["evidence_source"] == "pg_stat_wal_receiver"


def test_prepare_bundle_keeps_original_role_metadata_but_not_derived_edge(db):
    original = discovery.build_internal_discovery(db)
    prepared = prepare_discovery_relations(original)
    assert len(original.relations) == 5
    assert len(prepared.relations) == 4
    roles = {a["attributes"]["role_type"] for a in prepared.assets if a["asset_type"] == "database_instance"}
    assert roles == {"primary", "replica"}


def test_reprojection_preserves_real_manual_and_observed_normalized_relations(db):
    service.apply_internal_discovery(db)
    nodes = db.query(AssetInstance).order_by(AssetInstance.id).all()
    canonical = f"instance:{nodes[0].id}:replicates_to:instance:{nodes[1].id}"
    row = db.query(AssetRelation).filter_by(relation_key=canonical).one()
    row.attributes = {"provenance": "observed", "upstream": {"host": "198.51.100.152", "port": 3384}}
    row.status = "active"
    manual = AssetRelation(relation_key="manual-reverse", source_type="instance", source_id=nodes[1].id,
                           target_type="instance", target_id=nodes[0].id, relation_type="replicates_to",
                           discovery_source="manual", status="active", attributes={"provenance": "manual"})
    db.add(manual)
    db.commit()
    original = {r.relation_key: (r.status, deepcopy(r.attributes), r.discovery_source)
                for r in db.query(AssetRelation).filter_by(relation_type="replicates_to").all()}
    service.apply_internal_discovery(db)
    current = {r.relation_key: (r.status, r.attributes, r.discovery_source)
               for r in db.query(AssetRelation).filter_by(relation_type="replicates_to").all()}
    assert current == original


def test_failed_relation_guard_cannot_publish_role_derived_projection(db):
    source = service._ensure_internal_source(db)
    bundle = prepare_discovery_relations(discovery.build_internal_discovery(db))
    service.upsert_assets(db, source.id, bundle.assets, authoritative_snapshot=True)
    commits = []

    def committed(connection):
        commits.append(True)

    event.listen(db.get_bind(), "commit", committed)
    try:
        with patch.object(service, "reconcile_normalized_relations", side_effect=RuntimeError("synthetic relation guard failure")):
            with pytest.raises(RuntimeError, match="synthetic relation guard failure"):
                service.sync_normalized_asset_model(db, source.id)
        assert commits == []
        assert db.query(AssetInstance).count() == 0
        assert db.query(AssetRelation).count() == 0
    finally:
        event.remove(db.get_bind(), "commit", committed)


def test_disabled_saved_relation_retires_its_normalized_projection(db):
    service.apply_internal_discovery(db)
    assets = db.query(CmdbAsset).filter_by(asset_type="database_instance").order_by(CmdbAsset.id).all()
    saved = CmdbRelation(source_id=assets[0].source_id, external_id="explicit-endpoint",
                         source_asset_id=assets[0].id, target_asset_id=assets[1].id,
                         relation_type="replicates_to", status="active", raw_payload={"provenance": "manual"})
    db.add(saved)
    db.commit()
    service.sync_normalized_asset_model(db, saved.source_id)
    key = f"cmdb-relation:{saved.id}:replicates_to"
    assert db.query(AssetRelation).filter_by(relation_key=key).one().status == "active"
    saved.status = "inactive"
    db.commit()
    service.sync_normalized_asset_model(db, saved.source_id)
    assert db.query(AssetRelation).filter_by(relation_key=key).one().status == "inactive"
    assert not service.topology_groups(db)["groups"][0]["edges"]


def test_legacy_non_object_relation_attributes_are_preserved_without_crashing(db):
    service.apply_internal_discovery(db)
    row = db.query(AssetRelation).filter_by(relation_type="replicates_to").one()
    row.attributes = ["legacy-format"]
    row.status = "inactive"
    db.commit()
    service.sync_normalized_asset_model(db, None)
    db.refresh(row)
    assert row.attributes == ["legacy-format"]
    assert row.status == "inactive"


@pytest.mark.parametrize("db_type", ["mysql", "postgresql"])
def test_direct_peer_evidence_survives_recursive_discovery_and_failed_reprobe(db, db_type):
    db.query(DBInstance).filter(DBInstance.id != 1).delete()
    root = db.get(DBInstance, 1)
    root.db_type = db_type
    root.port = 3306 if db_type == "mysql" else 5432
    db.commit()
    hosts = ["198.51.100.152", "198.51.100.154", "198.51.100.153"]
    profiles = {}
    for index, host in enumerate(hosts):
        architecture = {
            "architecture_type": "replication" if db_type == "mysql" else "streaming_replication",
            "role_type": "primary" if index == 0 else "replica",
            "topology_group": "observed-cascade", "server_uuid": f"uuid-{index}",
            "members": [] if index == 2 else [{"host": hosts[index + 1], "port": root.port,
                                               "server_uuid": f"uuid-{index + 1}", "role_type": "replica"}],
        }
        if db_type == "postgresql":
            architecture["system_identifier"] = "observed-physical-cluster"
        profiles[host] = {"architecture": architecture}
    failing_hosts = set()

    def probe(instance):
        if instance.host in failing_hosts:
            raise RuntimeError("synthetic peer probe failure")
        return deepcopy(profiles[instance.host])

    def edges():
        group = service.topology_groups(db)["groups"][0]
        addresses = {node["id"]: node["host"] for node in group["nodes"]}
        return {(addresses[edge["source"]], addresses[edge["target"]]) for edge in group["edges"]}

    with patch.object(service.instance_service, "test_connection", side_effect=probe), \
            patch.object(service, "_probe_internal_hosts", return_value=({}, [])):
        service.apply_internal_discovery(db, probe_live=True)
        assert edges() == set()
        for index in (1, 2):
            profile = profiles[hosts[index]]["architecture"]
            if db_type == "mysql":
                profile.update(source_host=hosts[index - 1], source_port=root.port, source_uuid=f"uuid-{index - 1}")
            else:
                profile["in_recovery"] = True
                profile["members"].append({"slot_name": "receiver", "host": hosts[index - 1], "port": root.port})
            profile["conninfo"] = "password=must-not-be-retained"
        for _ in range(2):
            service.apply_internal_discovery(db, probe_live=True)
            assert edges() == {(hosts[0], hosts[1]), (hosts[1], hosts[2])}
            normalized = db.query(AssetRelation).filter_by(relation_type="replicates_to", status="active").all()
            assert len(normalized) == 2
            assert "must-not-be-retained" not in str([row.attributes for row in normalized])
        failing_hosts.add(hosts[2])
        service.apply_internal_discovery(db, probe_live=True)
        assert edges() == {(hosts[0], hosts[1])}
        assert db.query(AssetRelation).filter_by(relation_type="replicates_to", status="active").count() == 1
