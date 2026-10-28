"""Offline delivery gate for the four-engine discovery-to-asset path.

The profiles below use fields emitted by the engine collectors, while the
collector boundary itself is synthetic.  These tests intentionally exercise
``apply_internal_discovery(probe_live=True)`` instead of private probe helpers.
"""

from __future__ import annotations

import json
import socket
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cmdb import service
from app.cmdb.api import asset_router
from app.cmdb.models import AssetRelation, CmdbAsset, CmdbRelation
from app.core.database import get_db
from app.models.db_instance import DBInstance
from test_asset_center import MODELS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


ENGINE_CASES = {
    "mysql": {
        "port": 3306,
        "seed_host": "198.51.100.10",
        "peer_host": "198.51.100.11",
        "seed_profile": {
            "architecture_type": "replication",
            "role_type": "primary",
            "topology_group": "mysql:replication:11111111-1111-1111-1111-111111111111",
            "cluster_name": "mysql-delivery",
            "hostname": "mysql-primary",
            "server_id": 1,
            "server_uuid": "11111111-1111-1111-1111-111111111111",
            "member_key": "11111111-1111-1111-1111-111111111111",
            "members": [{
                "member_key": "22222222-2222-2222-2222-222222222222",
                "server_id": 2,
                "server_uuid": "22222222-2222-2222-2222-222222222222",
                "name": "mysql-replica",
                "host": "198.51.100.11",
                "reported_host": "198.51.100.11",
                "port": 3306,
                "role_type": "replica",
                # SHOW REPLICAS cannot establish the peer's health by itself.
                "health_status": "unknown",
            }],
        },
        "peer_profile": {
            "architecture_type": "replication",
            "role_type": "replica",
            "topology_group": "mysql:replication:11111111-1111-1111-1111-111111111111",
            "hostname": "mysql-replica",
            "server_id": 2,
            "server_uuid": "22222222-2222-2222-2222-222222222222",
            "member_key": "22222222-2222-2222-2222-222222222222",
            "upstream_host": "198.51.100.10",
            "upstream_port": 3306,
            "upstream_uuid": "11111111-1111-1111-1111-111111111111",
            "upstream_server_id": 1,
            "members": [],
        },
        "bad_identity": ("server_uuid", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
    },
    "oracle": {
        "port": 1521,
        "seed_host": "198.51.100.20",
        "peer_host": "198.51.100.21",
        "seed_profile": {
            "architecture_type": "data_guard",
            "role_type": "primary",
            "topology_group": "oracle:data_guard:main:reportdb",
            "cluster_name": "oracle-delivery",
            "db_unique_name": "main",
            "database_name": "MAIN",
            "instance_name": "main1",
            "member_key": "main",
            "members": [{
                "member_key": "reportdb",
                "name": "reportdb",
                "host": "198.51.100.21",
                "reported_host": "oracle-standby",
                "port": 1521,
                "role_type": "standby",
                "health_status": "healthy",
                "db_unique_name": "reportdb",
                "oracle_connect_type": "service_name",
                "service_name": "reportdb",
                "endpoint_unresolved": False,
                # v$archive_dest_status proves an outgoing transport target.
                "transport_source_db_unique_name": "main",
                "transport_destination_id": 2,
            }],
        },
        "peer_profile": {
            "architecture_type": "data_guard",
            "role_type": "standby",
            "topology_group": "oracle:data_guard:main:reportdb",
            "db_unique_name": "reportdb",
            "database_name": "REPORTDB",
            "instance_name": "report1",
            "member_key": "reportdb",
            # The collector derives this from v$dataguard_config.parent_dbun.
            "upstream_db_unique_name": "main",
            "members": [],
        },
        "bad_identity": ("db_unique_name", "wrongdb"),
    },
    "postgresql": {
        "port": 5432,
        "seed_host": "198.51.100.30",
        "peer_host": "198.51.100.31",
        "seed_profile": {
            "architecture_type": "streaming_replication",
            "role_type": "primary",
            "topology_group": "postgresql:streaming:7555000111222333444",
            "cluster_name": "postgresql-delivery",
            "host_ip": "198.51.100.30",
            "port": 5432,
            "system_identifier": "7555000111222333444",
            "member_key": "198.51.100.30:5432",
            "in_recovery": False,
            "members": [{
                "member_key": "198.51.100.31:5432",
                "name": "pg-replica",
                "host": "198.51.100.31",
                "reported_host": "pg-replica",
                "port": 5432,
                "port_inferred": True,
                "role_type": "replica",
                "health_status": "unknown",
                "member_state": "streaming",
            }],
        },
        "peer_profile": {
            "architecture_type": "streaming_replication",
            "role_type": "replica",
            "topology_group": "postgresql:streaming:7555000111222333444",
            "host_ip": "198.51.100.31",
            "port": 5432,
            "system_identifier": "7555000111222333444",
            "member_key": "198.51.100.31:5432",
            "in_recovery": True,
            "upstream_host": "198.51.100.30",
            "upstream_port": 5432,
            "members": [],
        },
        "bad_identity": ("system_identifier", "9999999999999999999"),
    },
    "sqlserver": {
        "port": 1433,
        "seed_host": "sql-primary.example",
        "peer_host": "sql-replica.example",
        "seed_profile": {
            "architecture_type": "always_on",
            "role_type": "primary",
            "topology_group": "sqlserver:always_on:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "cluster_name": "sqlserver-delivery",
            "server_name": "sql-primary.example",
            "replica_id": "11111111-aaaa-aaaa-aaaa-111111111111",
            "availability_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "availability_group_name": "MES-AG",
            "member_key": "11111111-aaaa-aaaa-aaaa-111111111111",
            "members": [{
                "member_key": "22222222-bbbb-bbbb-bbbb-222222222222",
                "name": "sql-replica.example",
                "host": "sql-replica.example",
                "reported_host": "sql-replica.example",
                "server_name": "sql-replica.example",
                "port": 1433,
                "role_type": "replica",
                "health_status": "healthy",
                "replica_id": "22222222-bbbb-bbbb-bbbb-222222222222",
                "availability_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            }],
        },
        "peer_profile": {
            "architecture_type": "always_on",
            "role_type": "replica",
            "topology_group": "sqlserver:always_on:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "server_name": "sql-replica.example",
            "replica_id": "22222222-bbbb-bbbb-bbbb-222222222222",
            "availability_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "availability_group_name": "MES-AG",
            "member_key": "22222222-bbbb-bbbb-bbbb-222222222222",
            # Deliberately no upstream_host: the SQL Server collector does not emit it.
            "upstream_server_name": "sql-primary.example",
            "members": [],
        },
        "bad_identity": ("replica_id", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
    },
}


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for model in MODELS:
        model.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    try:
        yield session_factory
    finally:
        engine.dispose()


def _seed_instance(db, engine_name: str, spec: dict) -> DBInstance:
    oracle = engine_name == "oracle"
    row = DBInstance(
        instance_name=f"{engine_name}-seed",
        db_type=engine_name,
        host=spec["seed_host"],
        port=spec["port"],
        username=f"{engine_name}-monitor",
        password_cipher=f"native-db-secret-{engine_name}",
        database_name="master" if engine_name == "sqlserver" else engine_name,
        system_name=f"{engine_name}-delivery",
        oracle_connect_type="service_name" if oracle else None,
        service_name="main" if oracle else None,
        enabled=1,
    )
    db.add(row)
    db.flush()
    return row


def _forbid_network(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("offline discovery gate attempted a socket connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    host_probe = Mock(side_effect=AssertionError("offline discovery gate attempted SSH"))
    monkeypatch.setattr(service.host_overview, "get_instance_host_overview", host_probe)
    return attempts, host_probe


def _client(session_factory):
    app = FastAPI()
    app.include_router(asset_router, prefix="/api/v1")

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _collector_stub(seed_ids: dict[str, int], *, bad_engine: str | None = None):
    def collect(instance: DBInstance):
        engine_name = str(instance.db_type).lower()
        spec = ENGINE_CASES[engine_name]
        if instance.id == seed_ids.get(engine_name):
            profile = deepcopy(spec["seed_profile"])
        else:
            assert instance.id is None, "discovery must not persist a native peer connection"
            assert instance.host == spec["peer_host"]
            profile = deepcopy(spec["peer_profile"])
            if engine_name == bad_engine:
                field, value = spec["bad_identity"]
                profile[field] = value
        return {"version": f"synthetic-{engine_name}", "architecture": profile, "warnings": []}

    return collect


def _active_replication_relations(db):
    return db.query(CmdbRelation).filter(
        CmdbRelation.status == "active",
        CmdbRelation.relation_type == "replicates_to",
    ).all()


def _build_frontend_topologies(api_payloads):
    script = """
import { buildAssetTopology } from './frontend/src/AssetTopology.js';
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payloads = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const output = payloads.map(({ cluster, members }) => {
  const topology = buildAssetTopology(cluster, members);
  return {
    edges: topology.edges,
    node_ids: Object.keys(topology.positions).sort(),
    has_cluster_node: topology.hasClusterNode,
  };
});
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(api_payloads),
        text=True,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    return json.loads(completed.stdout)


def test_four_engine_delivery_path_persists_api_arrows_idempotently_without_credentials(
    database,
    monkeypatch,
):
    attempts, host_probe = _forbid_network(monkeypatch)
    with database() as db:
        seeds = {
            engine_name: _seed_instance(db, engine_name, spec)
            for engine_name, spec in ENGINE_CASES.items()
        }
        db.commit()
        seed_ids = {name: row.id for name, row in seeds.items()}
        native_before = sorted(
            (row.id, row.db_type, row.host, row.username, row.password_cipher)
            for row in db.query(DBInstance).all()
        )
        monkeypatch.setattr(
            service.instance_service,
            "test_connection",
            _collector_stub(seed_ids),
        )

        first = service.apply_internal_discovery(db, probe_live=True)
        first_asset_count = db.query(CmdbAsset).count()
        first_relation_ids = sorted(row.external_id for row in _active_replication_relations(db))
        second = service.apply_internal_discovery(db, probe_live=True)

        assert first["probe_live"] is True and first["detected_architecture_count"] == 4
        assert first["normalized"]["instance_count"] == 8
        assert second["applied_created_count"] == 0
        assert second["normalized"]["instance_count"] == 8
        assert db.query(CmdbAsset).count() == first_asset_count
        assert sorted(row.external_id for row in _active_replication_relations(db)) == first_relation_ids
        assert len(first_relation_ids) == 4
        assert db.query(AssetRelation).filter(
            AssetRelation.status == "active",
            AssetRelation.relation_type == "replicates_to",
        ).count() == 4
        assert sorted(
            (row.id, row.db_type, row.host, row.username, row.password_cipher)
            for row in db.query(DBInstance).all()
        ) == native_before

        evidence_by_engine = {}
        assets = {row.id: row for row in db.query(CmdbAsset).all()}
        for relation in _active_replication_relations(db):
            engine_name = (assets[relation.source_asset_id].attributes or {})["db_type"]
            evidence_by_engine[engine_name] = relation.raw_payload
        assert evidence_by_engine["oracle"]["evidence_source"] in {
            "v$dataguard_config",
            "v$archive_dest_status",
        }
        assert evidence_by_engine["sqlserver"]["evidence_source"] == (
            "sys.dm_hadr_availability_group_states"
        )
        assert evidence_by_engine["sqlserver"]["upstream"] == {
            "server_name": "sql-primary.example",
            "availability_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }

        persisted = json.dumps(
            [{"raw_payload": row.raw_payload, "attributes": row.attributes} for row in assets.values()],
            default=str,
        )
        for engine_name in ENGINE_CASES:
            assert f"native-db-secret-{engine_name}" not in persisted

    with _client(database) as client:
        response = client.get("/api/v1/assets/topology")
        assert response.status_code == 200
        payload = response.json()
        assert payload["group_count"] == 4
        assert payload["node_count"] == 8
        assert {group["db_type"] for group in payload["groups"]} == set(ENGINE_CASES)
        for group in payload["groups"]:
            assert group["node_count"] == 2
            assert len(group["edges"]) == 1
            nodes = {node["id"]: node for node in group["nodes"]}
            edge = group["edges"][0]
            assert nodes[edge["source"]]["role"] == "primary"
            assert nodes[edge["target"]]["role"] in {"replica", "standby"}
            assert group["replication_state"] == "verified"
        serialized_api = json.dumps(payload, ensure_ascii=False)
        assert "native-db-secret" not in serialized_api

        clusters_response = client.get("/api/v1/assets/clusters")
        instances_response = client.get("/api/v1/assets/instances")
        assert clusters_response.status_code == instances_response.status_code == 200
        clusters = clusters_response.json()["items"]
        instances = instances_response.json()["items"]
        for cluster in clusters:
            diagnostic = client.get(f"/api/v1/assets/clusters/{cluster['id']}/ha-diagnostics")
            assert diagnostic.status_code == 200
            assert diagnostic.json()["summary"]["replication_state"] == "verified"
        frontend_inputs = [
            {
                "cluster": cluster,
                "members": [item for item in instances if item["cluster_id"] == cluster["id"]],
            }
            for cluster in clusters
        ]
        assert len(frontend_inputs) == 4
        frontend_outputs = _build_frontend_topologies(frontend_inputs)
        normalized_api_pairs = {
            (str(relation["source_id"]), str(relation["target_id"]))
            for cluster in clusters
            for relation in cluster["relations"]
            if relation["relation_type"] == "replicates_to"
        }
        frontend_pairs = {
            (edge["source"], edge["target"])
            for output in frontend_outputs
            for edge in output["edges"]
        }
        assert frontend_pairs == normalized_api_pairs
        for frontend_input, output in zip(frontend_inputs, frontend_outputs):
            expected_node_ids = sorted(str(item["id"]) for item in frontend_input["members"])
            assert output["node_ids"] == expected_node_ids
            assert len(output["edges"]) == 1
            assert output["has_cluster_node"] is False

    assert attempts == []
    host_probe.assert_not_called()


@pytest.mark.parametrize("engine_name", tuple(ENGINE_CASES))
def test_identity_mismatch_never_becomes_healthy_or_directional(
    database,
    monkeypatch,
    engine_name,
):
    attempts, host_probe = _forbid_network(monkeypatch)
    spec = ENGINE_CASES[engine_name]
    with database() as db:
        seed = _seed_instance(db, engine_name, spec)
        db.commit()
        monkeypatch.setattr(
            service.instance_service,
            "test_connection",
            _collector_stub({engine_name: seed.id}, bad_engine=engine_name),
        )

        result = service.apply_internal_discovery(db, probe_live=True)

        assert result["failed_probe_count"] == 0  # seed login succeeded
        assert result["warning_count"] >= 1
        assert _active_replication_relations(db) == []
        assert db.query(AssetRelation).filter(
            AssetRelation.status == "active",
            AssetRelation.relation_type == "replicates_to",
        ).count() == 0
        peer_assets = [
            row for row in db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance")
            if (row.attributes or {}).get("native_type") == "discovered_database_instance"
        ]
        assert len(peer_assets) == 1
        peer = peer_assets[0]
        assert peer.attributes["discovery_status"] == "failed"
        assert peer.attributes["health_status"] != "healthy"
        assert peer.attributes.get("replication", {}).get("state") != "verified"

    with _client(database) as client:
        group = client.get("/api/v1/assets/topology").json()["groups"][0]
        assert group["edges"] == []
        peer_node = next(node for node in group["nodes"] if node["role"] != "primary")
        assert peer_node["health_status"] != "healthy"

    assert attempts == []
    host_probe.assert_not_called()


@pytest.mark.parametrize("engine_name", tuple(ENGINE_CASES))
def test_collector_member_without_login_endpoint_never_gets_fake_ip_or_success(
    database,
    monkeypatch,
    engine_name,
):
    attempts, host_probe = _forbid_network(monkeypatch)
    spec = deepcopy(ENGINE_CASES[engine_name])
    member = spec["seed_profile"]["members"][0]
    member["host"] = ""
    member["reported_host"] = ""
    member["health_status"] = "unknown"
    if engine_name == "oracle":
        member.update(
            connect_identifier="REPORTDB_ALIAS",
            endpoint_unresolved=True,
            service_name="",
        )
        monkeypatch.setattr(service, "resolve_oracle_aliases", lambda *_: {})
    elif engine_name == "postgresql":
        member.update(
            member_key="postgresql-application:pg-replica",
            application_name="pg-replica",
            name="pg-replica",
            port_inferred=True,
        )
    elif engine_name == "sqlserver":
        member.update(
            server_name="",
            name="sql-replica",
            port_inferred=True,
            identity_verification_required=True,
        )

    with database() as db:
        seed = _seed_instance(db, engine_name, spec)
        db.commit()

        def collect(instance):
            assert instance.id == seed.id, "an unresolved endpoint must not be probed"
            return {
                "version": f"synthetic-{engine_name}",
                "architecture": deepcopy(spec["seed_profile"]),
                "warnings": [],
            }

        collector = Mock(side_effect=collect)
        monkeypatch.setattr(service.instance_service, "test_connection", collector)

        service.apply_internal_discovery(db, probe_live=True)

        assert collector.call_count == 1
        assert _active_replication_relations(db) == []
        peers = [
            row for row in db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance")
            if (row.attributes or {}).get("native_type") == "discovered_database_instance"
        ]
        assert len(peers) == 1
        attributes = peers[0].attributes
        assert attributes["host"] == ""
        assert attributes["discovery_status"] != "success"
        assert attributes["health_status"] != "healthy"
        assert attributes["db_probe_status"] == "needs_endpoint"

    with _client(database) as client:
        group = client.get("/api/v1/assets/topology").json()["groups"][0]
        assert group["edges"] == []
        unresolved = next(node for node in group["nodes"] if node["role"] != "primary")
        assert unresolved["host"] in {None, ""}
        assert unresolved["health_status"] != "healthy"

    assert attempts == []
    host_probe.assert_not_called()


@pytest.mark.parametrize("engine_name", tuple(ENGINE_CASES))
def test_verified_members_without_direction_evidence_do_not_invent_arrows(
    database,
    monkeypatch,
    engine_name,
):
    attempts, host_probe = _forbid_network(monkeypatch)
    spec = deepcopy(ENGINE_CASES[engine_name])
    peer_profile = spec["peer_profile"]
    for field in (
        "upstream_host",
        "upstream_port",
        "upstream_uuid",
        "upstream_server_id",
        "upstream_db_unique_name",
        "upstream_server_name",
    ):
        peer_profile.pop(field, None)
    if engine_name == "oracle":
        member = spec["seed_profile"]["members"][0]
        member.pop("transport_source_db_unique_name", None)
        member.pop("transport_destination_id", None)

    with database() as db:
        seed = _seed_instance(db, engine_name, spec)
        db.commit()

        def collect(instance):
            profile = spec["seed_profile"] if instance.id == seed.id else peer_profile
            return {
                "version": f"synthetic-{engine_name}",
                "architecture": deepcopy(profile),
                "warnings": [],
            }

        monkeypatch.setattr(service.instance_service, "test_connection", collect)
        service.apply_internal_discovery(db, probe_live=True)

        peer = next(
            row for row in db.query(CmdbAsset).filter(CmdbAsset.asset_type == "database_instance")
            if (row.attributes or {}).get("native_type") == "discovered_database_instance"
        )
        assert peer.attributes["discovery_status"] == "success"
        assert peer.attributes["architecture_verified"] is True
        assert peer.attributes.get("replication", {}).get("state") != "verified"
        assert _active_replication_relations(db) == []

    with _client(database) as client:
        group = client.get("/api/v1/assets/topology").json()["groups"][0]
        assert group["node_count"] == 2
        assert group["edges"] == []
        assert group["replication_state"] == "unknown"

    assert attempts == []
    host_probe.assert_not_called()
