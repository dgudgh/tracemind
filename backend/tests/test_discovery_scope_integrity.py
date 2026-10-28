"""Offline invariants for identity, scope isolation, and projected health."""

from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cmdb import service
from app.cmdb.models import AssetCluster, AssetHost, AssetInstance, CmdbAsset, CmdbRelation
from app.cmdb.peer_connection import verify_peer_identity
from app.models.db_instance import DBInstance
from test_asset_center import MODELS


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for model in MODELS:
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(*, name: str, tenant_id: int, organization_id: int, host: str = "10.0.0.10") -> DBInstance:
    return DBInstance(
        instance_name=name,
        db_type="mysql",
        host=host,
        port=3306,
        username="monitor",
        password_cipher="offline-secret",
        architecture_type="replication",
        role_type="primary",
        tenant_id=tenant_id,
        organization_id=organization_id,
        enabled=1,
    )


def _profile(peer_host: str = "10.0.0.11") -> dict:
    return {
        "architecture_type": "replication",
        "role_type": "primary",
        "topology_group": "mysql:replication:shared-group",
        "cluster_name": "shared-name",
        "server_uuid": "seed-uuid",
        "member_key": "seed-uuid",
        "health_status": "healthy",
        "discovery_status": "success",
        "members": [{
            "member_key": "shared-peer-uuid",
            "server_uuid": "shared-peer-uuid",
            "server_id": 2,
            "name": "mysql-peer",
            "host": peer_host,
            "reported_host": peer_host,
            "port": 3306,
            "role_type": "replica",
            "health_status": "healthy",
            "direct_probe": True,
            "db_probe_status": "verified",
            "replication": {
                "state": "verified",
                "upstream": {"server_uuid": "seed-uuid"},
            },
        }],
    }


def test_mysql_identity_prefers_uuid_and_requires_observed_identity():
    expected = {"server_uuid": "uuid-a", "server_id": 7}
    verify_peer_identity("mysql", expected, {"server_uuid": "uuid-a", "server_id": 99}, {})

    with pytest.raises(ValueError):
        verify_peer_identity("mysql", expected, {"server_id": 7}, {})
    with pytest.raises(ValueError):
        verify_peer_identity("mysql", expected, {"server_uuid": "uuid-b", "server_id": 7}, {})

    fallback = {"server_id": 7}
    verify_peer_identity("mysql", fallback, {"server_id": 7}, {})
    with pytest.raises(ValueError):
        verify_peer_identity("mysql", fallback, {}, {})

    # An existing explicitly configured/authenticated connection with no seed
    # identity remains compatible; the discovery orchestrator marks only an
    # unconfigured peer unverified.
    verify_peer_identity("mysql", {}, {"server_uuid": "observed-only"}, {})


def test_same_private_ips_and_database_identity_remain_isolated_by_scope(db, monkeypatch):
    first = _seed(name="tenant-a", tenant_id=1, organization_id=10)
    second = _seed(name="tenant-b", tenant_id=2, organization_id=20)
    db.add_all([first, second])
    db.commit()
    profiles = {first.id: _profile(), second.id: _profile()}

    monkeypatch.setattr(service, "_probe_internal_instances", lambda _: (deepcopy(profiles), [], 2))
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *_: ({}, []))

    result = service.apply_internal_discovery(db, probe_live=True)

    assert result["normalized"]["instance_count"] == 4
    database_assets = db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance").all()
    peers = [
        row for row in database_assets
        if (row.attributes or {}).get("native_type") == "discovered_database_instance"
    ]
    assert len(peers) == 2
    assert len({row.external_id for row in peers}) == 2
    assert {
        ((row.attributes or {}).get("tenant_id"), (row.attributes or {}).get("organization_id"))
        for row in peers
    } == {(1, 10), (2, 20)}

    hosts = db.query(AssetHost).all()
    peer_hosts = [row for row in hosts if row.ip == "10.0.0.11"]
    assert len(peer_hosts) == 2
    assert len({row.stable_key for row in peer_hosts}) == 2
    assert {(row.tenant_id, row.organization_id) for row in peer_hosts} == {(1, 10), (2, 20)}

    clusters = db.query(AssetCluster).all()
    assert len(clusters) == 2
    assert len({row.stable_key for row in clusters}) == 2
    assert {(row.tenant_id, row.organization_id) for row in clusters} == {(1, 10), (2, 20)}

    normalized_instances = db.query(AssetInstance).all()
    for item in normalized_instances:
        cluster = db.get(AssetCluster, item.cluster_id)
        host = db.get(AssetHost, item.host_id)
        assert (item.tenant_id, item.organization_id) == (
            cluster.tenant_id,
            cluster.organization_id,
        )
        assert (item.tenant_id, item.organization_id) == (
            host.tenant_id,
            host.organization_id,
        )
    assert db.query(CmdbRelation).filter(CmdbRelation.relation_type == "replicates_to").count() == 2


def test_configured_native_match_does_not_cross_scope(db, monkeypatch):
    source = _seed(name="tenant-a-source", tenant_id=1, organization_id=10)
    foreign = _seed(name="tenant-b-peer", tenant_id=2, organization_id=20, host="10.0.0.11")
    db.add_all([source, foreign])
    db.commit()
    transient_calls: list[str] = []

    source_profile = _profile()
    foreign_profile = {
        "architecture_type": "replication",
        "role_type": "replica",
        "topology_group": "mysql:replication:peer-local-view",
        "server_uuid": "shared-peer-uuid",
        "member_key": "shared-peer-uuid",
        "health_status": "healthy",
        "members": [],
    }

    def collect(instance: DBInstance):
        if instance.id == source.id:
            architecture = deepcopy(source_profile)
        elif instance.id == foreign.id:
            architecture = deepcopy(foreign_profile)
        else:
            assert instance.id is None
            transient_calls.append(instance.host)
            architecture = deepcopy(foreign_profile)
        return {"version": "offline", "architecture": architecture, "warnings": []}

    monkeypatch.setattr(service.instance_service, "test_connection", collect)
    profiles, _, _ = service._probe_internal_instances(db)

    member = profiles[source.id]["members"][0]
    assert transient_calls == ["10.0.0.11"]
    assert member["db_probe_status"] == "verified"
    assert member.get("managed_instance_id") is None
    assert profiles[source.id]["topology_group"] == "mysql:replication:shared-group"


def test_global_scope_discovered_peer_keeps_legacy_external_id(db, monkeypatch):
    seed = _seed(name="legacy-global", tenant_id=None, organization_id=None)
    db.add(seed)
    db.commit()
    profile = _profile()
    monkeypatch.setattr(service, "_probe_internal_instances", lambda _: ({seed.id: profile}, [], 1))
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *_: ({}, []))

    service.apply_internal_discovery(db, probe_live=True)

    peer = next(
        row for row in db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance")
        if (row.attributes or {}).get("native_type") == "discovered_database_instance"
    )
    legacy_digest = hashlib.sha256(b"shared-peer-uuid").hexdigest()[:16]
    assert peer.external_id == f"db-discovered:mysql:{legacy_digest}"
    assert db.query(AssetHost).filter(AssetHost.ip == "10.0.0.11").one().stable_key == "host:10.0.0.11"


def test_mysql_endpoint_login_without_seed_identity_remains_unverified(db, monkeypatch):
    seed = _seed(name="identity-missing", tenant_id=1, organization_id=10)
    db.add(seed)
    db.commit()
    source_profile = _profile()
    member = source_profile["members"][0]
    member.pop("member_key")
    member.pop("server_uuid")
    member.pop("server_id")

    def collect(instance: DBInstance):
        architecture = source_profile if instance.id == seed.id else {
            "architecture_type": "replication",
            "role_type": "replica",
            "topology_group": "mysql:replication:peer-local-view",
            "server_uuid": "observed-but-not-seed-proven",
            "members": [],
        }
        return {"version": "offline", "architecture": deepcopy(architecture), "warnings": []}

    monkeypatch.setattr(service.instance_service, "test_connection", collect)
    profiles, warnings, _ = service._probe_internal_instances(db)

    discovered = profiles[seed.id]["members"][0]
    assert discovered["direct_probe"] is False
    assert discovered["db_probe_status"] == "unverified"
    assert "server_uuid/server_id" in discovered["db_probe_reason"]
    assert any("身份" in warning for warning in warnings)


def test_peer_probe_limit_records_not_attempted_reason(db, monkeypatch):
    seed = _seed(name="probe-limit", tenant_id=1, organization_id=10)
    db.add(seed)
    db.commit()
    source_profile = _profile()
    source_profile["members"].append({
        "member_key": "second-peer-uuid",
        "server_uuid": "second-peer-uuid",
        "host": "10.0.0.12",
        "reported_host": "10.0.0.12",
        "port": 3306,
        "role_type": "replica",
        "health_status": "unknown",
    })

    def collect(instance: DBInstance):
        if instance.id == seed.id:
            architecture = source_profile
        else:
            architecture = {
                "architecture_type": "replication",
                "role_type": "replica",
                "server_uuid": (
                    "shared-peer-uuid" if instance.host == "10.0.0.11" else "second-peer-uuid"
                ),
                "members": [],
            }
        return {"version": "offline", "architecture": deepcopy(architecture), "warnings": []}

    monkeypatch.setattr(service, "MAX_TOPOLOGY_PEER_PROBES", 1)
    monkeypatch.setattr(service.instance_service, "test_connection", collect)
    profiles, _, _ = service._probe_internal_instances(db)

    statuses = {member["db_probe_status"] for member in profiles[seed.id]["members"]}
    assert statuses == {"verified", "not_attempted"}
    skipped = next(
        member for member in profiles[seed.id]["members"]
        if member["db_probe_status"] == "not_attempted"
    )
    assert "上限 1" in skipped["db_probe_reason"]


def test_failed_direct_probe_cannot_project_healthy_and_keeps_transport_evidence(db, monkeypatch):
    seed = _seed(name="failed-peer-seed", tenant_id=7, organization_id=70)
    db.add(seed)
    db.commit()
    profile = _profile()
    member = profile["members"][0]
    member.update({
        "direct_probe": False,
        "db_probe_status": "failed",
        "probe_error": "offline synthetic failure",
        "transport_source_db_unique_name": "PRIMARY",
        "transport_destination_id": 2,
    })
    monkeypatch.setattr(service, "_probe_internal_instances", lambda _: ({seed.id: profile}, [], 1))
    monkeypatch.setattr(service, "_probe_internal_hosts", lambda *_: ({}, []))

    service.apply_internal_discovery(db, probe_live=True)

    peer = next(
        row for row in db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance")
        if (row.attributes or {}).get("native_type") == "discovered_database_instance"
    )
    attributes = peer.attributes or {}
    assert attributes["health_status"] == "unknown"
    assert attributes["discovery_status"] == "failed"
    assert attributes["architecture_evidence"]["transport_source_db_unique_name"] == "PRIMARY"
    assert attributes["architecture_evidence"]["transport_destination_id"] == 2

