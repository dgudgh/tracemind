"""Offline acceptance of TNS alias -> verified DG member -> persisted topology."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import service, normalized_service
from app.cmdb.models import CmdbAsset, CmdbRelation, AssetRelation
from app.cmdb.replication import upstream_evidence
from app.cmdb.topology_relations import verified_pairs
from app.models.db_instance import DBInstance
from test_asset_center import MODELS


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in MODELS:
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def seed_and_profile(db):
    seed = DBInstance(instance_name="main", db_type="oracle", host="192.0.2.10", port=1521,
        username="synthetic", password_cipher="synthetic-cipher", enabled=1,
        account_type="system", oracle_connect_type="service_name", service_name="main",
        ssh_host="192.0.2.10", ssh_username="ops", ssh_password_cipher="synthetic-ssh")
    db.add(seed); db.commit()
    profile = {"architecture_type":"data_guard", "role_type":"primary", "db_unique_name":"main",
        "member_key":"main", "hostname":"main-host", "db_version":"19.27", "topology_group":"oracle:data_guard:main:reportdb",
        "members":[{"name":"reportdb", "member_key":"reportdb", "host":"", "reported_host":"", "port":1521,
            "db_unique_name":"reportdb", "role_type":"standby", "health_status":"healthy",
            "connect_identifier":"REPORTDB", "endpoint_unresolved":True,
            "transport_source_db_unique_name":"main", "transport_destination_id":2}]}
    return seed, profile


def wire(db, monkeypatch, *, unique_name="reportdb", resolved_host="192.0.2.11", resolved_port=1522, resolved_service="report_service"):
    seed, profile = seed_and_profile(db)
    resolver = Mock(return_value={"REPORTDB":{"host":resolved_host,"port":resolved_port,
        "oracle_connect_type":"service_name","service_name":resolved_service,"sid":"", "endpoint_unresolved":False}})
    monkeypatch.setattr(service, "resolve_oracle_aliases", resolver)
    connect = Mock(side_effect=lambda item: {"architecture": deepcopy(profile) if item.id == seed.id else {
        "architecture_type":"data_guard", "role_type":"standby", "db_unique_name":unique_name,
        "member_key":unique_name, "hostname":"report-host", "instance_name":"report1", "db_version":"19.27",
        "topology_group":profile["topology_group"], "members":[]}})
    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    return seed, resolver, connect


def test_alias_requires_database_identity_before_binding_and_uses_peer_service(db, monkeypatch):
    seed, resolver, connect = wire(db, monkeypatch)
    profiles, warnings, _ = service._probe_internal_instances(db)
    assert not warnings
    member = profiles[seed.id]["members"][0]
    assert (member["host"],member["port"],member["db_probe_status"]) == ("192.0.2.11",1522,"verified")
    peer = connect.call_args_list[1].args[0]
    assert (peer.service_name,peer.username,peer.password_cipher) == ("report_service",seed.username,seed.password_cipher)
    assert member["endpoint_source"] == "seed_tns_alias_identity"
    assert member["replication"] == {"state":"verified", "upstream":{"db_unique_name":"main"},
        "evidence_source":"v$archive_dest_status", "destination_id":2}
    resolver.assert_called_once()
    assert db.query(DBInstance).count() == 1


@pytest.mark.parametrize("unique_name,resolved_host,resolved_port,resolved_service", [("unrelated","192.0.2.11",1522,"report_service"),("reportdb","192.0.2.10",1521,"main")])
def test_mismatching_or_unverified_configured_endpoint_is_not_bound(db, monkeypatch, unique_name, resolved_host, resolved_port, resolved_service):
    seed, _, _ = wire(db, monkeypatch, unique_name=unique_name, resolved_host=resolved_host, resolved_port=resolved_port, resolved_service=resolved_service)
    profiles, _, _ = service._probe_internal_instances(db)
    member = profiles[seed.id]["members"][0]
    assert not member["host"] and member["endpoint_unresolved"] is True
    assert member["direct_probe"] is False
    assert member["db_probe_status"] == ("needs_endpoint" if resolved_host == seed.host else "failed")


@pytest.mark.parametrize("shared_listener", [False, True])
def test_dg_discovery_persists_one_arrow_without_new_native_credentials(db, monkeypatch, shared_listener):
    options = {"resolved_host":"192.0.2.10","resolved_port":1521} if shared_listener else {}
    seed, _, _ = wire(db, monkeypatch, **options)
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *args: ({}, []))
    for _ in range(2):
        service.apply_internal_discovery(db, probe_live=True)
        edges = db.query(CmdbRelation).filter_by(relation_type="replicates_to",status="active").all()
        assert len(edges) == 1
        assert edges[0].raw_payload["evidence_source"] == "v$archive_dest_status"
        normalized = db.query(AssetRelation).filter_by(relation_type="replicates_to",status="active").all()
        assert len(normalized) == 1
        cluster = normalized_service.list_clusters(db)[0]
        assert len([r for r in cluster["relations"] if r["relation_type"] == "replicates_to"]) == 1
        members = normalized_service.list_instances(db)
        assert len(members) == 2 and all(m["endpoint"] for m in members)
        assert {m["role"] for m in members} == {"adg_primary","adg_standby"}
    assert db.query(DBInstance).count() == 1
    stored = str([r.attributes for r in db.query(CmdbAsset)])
    assert "synthetic-cipher" not in stored and "synthetic-ssh" not in stored


def test_failed_native_profile_cannot_become_verified_managed_peer_by_endpoint(db, monkeypatch):
    seed, profile = seed_and_profile(db)
    stale = DBInstance(instance_name="stale",db_type="oracle",host="192.0.2.11",port=1521,
        username="synthetic",password_cipher="not-used",oracle_connect_type="service_name",service_name="reportdb",enabled=1)
    db.add(stale); db.commit()
    profile["members"][0].update(host=stale.host,port=stale.port,endpoint_unresolved=False,
        oracle_connect_type="service_name",service_name="reportdb")
    def connect(item):
        if item.id == seed.id:return {"architecture":deepcopy(profile)}
        raise RuntimeError("synthetic login failure")
    monkeypatch.setattr(service.instance_service,"test_connection",connect)
    profiles, _, _ = service._probe_internal_instances(db)
    peer = profiles[seed.id]["members"][0]
    assert not peer.get("managed_instance_id")
    assert peer["direct_probe"] is False and peer["db_probe_status"] == "failed"
    assert not peer["host"] and peer["endpoint_unresolved"] is True
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *args: ({}, []))
    service.apply_internal_discovery(db, probe_live=True)
    discovered = [r for r in db.query(CmdbAsset).filter_by(asset_type="database_instance")
                  if (r.attributes or {}).get("native_type") == "discovered_database_instance"]
    assert discovered and all(not row.attributes.get("host") for row in discovered)
    nodes = normalized_service.list_instances(db)
    assert len([row for row in nodes if row["endpoint"] == f"{stale.host}:{stale.port}"]) == 1


def test_unresolved_dg_member_retains_database_name_and_truthful_status_in_api(db, monkeypatch):
    _, resolver, _ = wire(db, monkeypatch)
    resolver.return_value = {}
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *args: ({}, []))
    service.apply_internal_discovery(db, probe_live=True)
    standby = next(m for m in normalized_service.list_instances(db) if m["role"] == "adg_standby")
    assert standby["name"] == "reportdb"
    assert standby["host_name"] is None and standby["endpoint"] == ""
    assert standby["status"] == "unknown"
    assert not db.query(AssetRelation).filter_by(relation_type="replicates_to",status="active").all()


def test_dg_parent_database_evidence_preserves_cascade_and_rejects_ambiguity():
    def node(number, name, parent=None):
        profile={"architecture_type":"data_guard", "upstream_db_unique_name":parent}
        return {"id":number,"external_id":f"db-{number}","source_id":1,"asset_type":"database_instance","status":"active",
            "attributes":{"db_type":"oracle","architecture_type":"data_guard","topology_group":"dg", "host":f"192.0.2.{number}","port":1521,
                "architecture_evidence":{"db_unique_name":name},"replication":upstream_evidence(profile)}}
    nodes=[node(1,"main"),node(2,"relay","main"),node(3,"report","relay")]
    assert [(s["id"],t["id"]) for s,t,_ in verified_pairs(nodes)] == [(1,2),(2,3)]
    nodes.append(node(4,"relay"))
    assert [(s["id"],t["id"]) for s,t,_ in verified_pairs(nodes)] == [(1,2)]
