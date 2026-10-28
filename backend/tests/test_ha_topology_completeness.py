"""Offline HA readiness tests for persisted topology evidence."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import ha_diagnostics
from app.cmdb.models import (
    AssetCluster,
    AssetClusterHealthSnapshot,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetRelation,
    AssetSite,
    AssetZone,
)


MODELS = (
    AssetRegion,
    AssetSite,
    AssetZone,
    AssetEnvironment,
    AssetHost,
    AssetCluster,
    AssetInstance,
    AssetRelation,
    AssetClusterHealthSnapshot,
)


@pytest.fixture
def topology_env():
    engine = create_engine("sqlite:///:memory:")
    for model in MODELS:
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    region = AssetRegion(code="test-region", name="test-region")
    session.add(region)
    session.flush()
    site = AssetSite(code="test-site", name="test-site", region_id=region.id)
    session.add(site)
    session.flush()
    zone = AssetZone(code="test-zone", name="test-zone", region_id=region.id, site_id=site.id)
    session.add(zone)
    session.flush()
    environment = AssetEnvironment(
        code="test-env", name="test-env", env_type="test",
        region_id=region.id, site_id=site.id, zone_id=zone.id,
    )
    session.add(environment)
    session.commit()
    location = {
        "region_id": region.id,
        "site_id": site.id,
        "zone_id": zone.id,
        "env_id": environment.id,
    }
    try:
        yield session, location
    finally:
        session.close()
        engine.dispose()


def add_cluster(db, location, name="cluster", architecture="primary_replica", db_type="MySQL"):
    row = AssetCluster(
        stable_key=f"cluster:{name}", name=name, db_type=db_type,
        cluster_type="database", architecture=architecture, status="healthy",
        discovery_source="automatic", attributes={}, **location,
    )
    db.add(row)
    db.commit()
    return row


def add_member(db, location, cluster, number, role, *, verified=True, attributes=None):
    ip = f"192.0.2.{number}"
    host = AssetHost(
        stable_key=f"host:{cluster.id}:{number}", name=f"node-{number}",
        hostname=f"node-{number}", ip=ip, status="healthy",
        discovery_source="automatic", **location,
    )
    db.add(host)
    db.flush()
    member = AssetInstance(
        stable_key=f"instance:{cluster.id}:{number}", name=f"node-{number}",
        db_type=cluster.db_type, ip=ip,
        port={"MySQL": 3306, "PostgreSQL": 5432, "Oracle": 1521, "SQLServer": 1433}[cluster.db_type],
        role=role, host_id=host.id, cluster_id=cluster.id, status="healthy",
        native_instance_id=1000 + number, discovery_source="automatic",
        attributes={
            "discovery_status": "success" if verified else "configured",
            "architecture_verified": verified,
            **(attributes or {}),
        },
        **location,
    )
    db.add(member)
    db.commit()
    return member


def add_relation(
    db, source, target, *, relation_type="replicates_to", status="active",
    discovery_source="automatic", attributes=None, target_type="instance",
):
    target_id = target.id
    row = AssetRelation(
        relation_key=f"test:{relation_type}:{source.id}:{target_id}:{len(db.new)}:{discovery_source}",
        source_type="instance", source_id=source.id,
        target_type=target_type, target_id=target_id,
        relation_type=relation_type, status=status,
        discovery_source=discovery_source,
        attributes=attributes if attributes is not None else {
            "discovery_method": "cmdb_relation_projection",
            "provenance": "observed",
            "evidence": {
                "state": "verified",
                "upstream": {"host": source.ip, "port": source.port},
            },
        },
    )
    db.add(row)
    db.commit()
    return row


def finding_codes(result):
    return {item["code"] for item in result["findings"]}


def test_missing_relation_is_unknown_warning_and_not_ready(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(db, primary, replica, status="inactive")

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    assert result["summary"]["health_status"] == "warning"
    assert "HA_TOPOLOGY_MISSING" in finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]
    assert next(item for item in result["findings"] if item["code"] == "HA_TOPOLOGY_MISSING")["severity"] == "warning"


def test_partial_and_disconnected_topology_are_not_ready(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_member(db, location, cluster, 3, "replica")
    add_relation(db, primary, replica)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "partial"
    assert "HA_TOPOLOGY_PARTIAL" in finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]


@pytest.mark.parametrize("member_count", [2, 3])
def test_complete_two_and_three_tier_chains_are_verified(topology_env, member_count):
    db, location = topology_env
    cluster = add_cluster(db, location)
    nodes = [add_member(db, location, cluster, 1, "primary")]
    nodes.extend(add_member(db, location, cluster, number, "replica") for number in range(2, member_count + 1))
    for source, target in zip(nodes, nodes[1:]):
        add_relation(db, source, target)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "verified"
    assert result["summary"]["health_status"] == "healthy"
    assert result["action_readiness"]["ready_for_plan"]


@pytest.mark.parametrize(("architecture", "db_type", "primary_role", "secondary_role"), [
    ("primary_replica", "MySQL", "primary", "replica"),
    ("streaming_replication", "PostgreSQL", "primary", "replica"),
    ("adg", "Oracle", "adg_primary", "adg_standby"),
    ("alwayson", "SQLServer", "alwayson_primary", "alwayson_secondary"),
])
def test_all_directional_architectures_require_and_accept_verified_edge(
    topology_env, architecture, db_type, primary_role, secondary_role,
):
    db, location = topology_env
    cluster = add_cluster(db, location, architecture=architecture, db_type=db_type)
    primary = add_member(db, location, cluster, 1, primary_role)
    secondary = add_member(db, location, cluster, 2, secondary_role)
    add_relation(db, primary, secondary)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "verified"
    assert result["action_readiness"]["ready_for_plan"]


@pytest.mark.parametrize("shape", ["reverse", "cycle"])
def test_reverse_and_cycle_are_ambiguous(topology_env, shape):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(db, replica, primary)
    if shape == "cycle":
        add_relation(db, primary, replica)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "partial"
    assert "HA_TOPOLOGY_AMBIGUOUS" in finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]


def test_cross_cluster_relation_is_isolated_from_readiness(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location, name="inside")
    primary = add_member(db, location, cluster, 1, "primary")
    add_member(db, location, cluster, 2, "replica")
    other = add_cluster(db, location, name="outside")
    outside = add_member(db, location, other, 3, "replica")
    add_relation(db, primary, outside)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    assert {"HA_TOPOLOGY_SCOPE_CONFLICT", "HA_TOPOLOGY_MISSING"} <= finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]


def test_manual_relation_is_visible_but_does_not_prove_readiness(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(
        db, primary, replica, discovery_source="manual",
        attributes={"provenance": "manual"},
    )

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    assert "HA_TOPOLOGY_MANUAL_ONLY" in finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]


def test_role_derived_relation_does_not_prove_readiness(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(
        db, primary, replica,
        attributes={"discovery_method": "database_role", "provenance": "derived"},
    )

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    missing = next(item for item in result["findings"] if item["code"] == "HA_TOPOLOGY_MISSING")
    assert missing["evidence"]["role_derived_relation_count"] == 1
    assert not result["action_readiness"]["ready_for_plan"]


def test_automatic_relation_without_observed_evidence_does_not_prove_readiness(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(db, primary, replica, discovery_source="automatic", attributes={})

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    assert "HA_TOPOLOGY_RELATION_UNVERIFIED" in finding_codes(result)
    missing = next(item for item in result["findings"] if item["code"] == "HA_TOPOLOGY_MISSING")
    assert missing["evidence"]["unverified_relation_count"] == 1
    assert not result["action_readiness"]["ready_for_plan"]


def test_explicit_observed_upstream_relation_is_accepted(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica")
    add_relation(db, primary, replica, attributes={
        "provenance": "observed",
        "upstream": {"host": primary.ip, "port": primary.port},
    })

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "verified"
    assert result["action_readiness"]["ready_for_plan"]


def test_verified_edge_with_unverified_endpoint_is_partial(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location)
    primary = add_member(db, location, cluster, 1, "primary")
    replica = add_member(db, location, cluster, 2, "replica", verified=False)
    add_relation(db, primary, replica)

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "partial"
    assert "HA_TOPOLOGY_ENDPOINT_UNVERIFIED" in finding_codes(result)
    assert not result["action_readiness"]["ready_for_plan"]


def test_mgr_requires_complete_membership_but_no_replication_arrows(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location, architecture="mgr")
    nodes = [
        add_member(db, location, cluster, 1, "primary"),
        add_member(db, location, cluster, 2, "replica"),
        add_member(db, location, cluster, 3, "replica"),
    ]
    missing = ha_diagnostics.assess_cluster(db, cluster.id)
    assert missing["summary"]["replication_state"] == "unknown"
    assert not missing["action_readiness"]["ready_for_plan"]

    for node in nodes:
        add_relation(db, node, cluster, relation_type="member_of", target_type="cluster")
    complete = ha_diagnostics.assess_cluster(db, cluster.id)
    assert complete["summary"]["replication_state"] == "verified"
    assert complete["action_readiness"]["ready_for_plan"]
    assert not db.query(AssetRelation).filter_by(relation_type="replicates_to").count()


def test_rac_with_dataguard_evidence_is_explicitly_unverified_composite(topology_env):
    db, location = topology_env
    cluster = add_cluster(db, location, architecture="rac", db_type="Oracle")
    add_member(
        db, location, cluster, 1, "primary",
        attributes={"architecture_evidence": {"has_dataguard": True}},
    )
    add_member(db, location, cluster, 2, "replica")

    result = ha_diagnostics.assess_cluster(db, cluster.id)

    assert result["summary"]["replication_state"] == "unknown"
    assert "HA_COMPOSITE_TOPOLOGY_UNVERIFIED" in finding_codes(result)
    assert result["summary"]["health_status"] == "warning"
    assert not result["action_readiness"]["ready_for_plan"]
