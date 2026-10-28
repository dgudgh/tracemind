"""Truthful projection tests for unresolved Oracle Data Guard members."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb.discovery import build_internal_discovery
from app.models.db_instance import DBInstance


def _discovery_bundle(*, direct_probe: bool):
    engine = create_engine("sqlite:///:memory:")
    DBInstance.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        seed = DBInstance(
            instance_name="mesdb",
            db_type="oracle",
            host="192.0.2.10",
            port=1521,
            username="monitor",
            password_cipher="synthetic-secret-reference",
            service_name="mesdb",
            enabled=1,
        )
        session.add(seed)
        session.commit()
        member = {
            "member_key": "reportdb",
            "name": "reportdb",
            "db_unique_name": "reportdb",
            "host": "",
            "reported_host": "",
            "port": 1521,
            "role_type": "standby",
            "health_status": "healthy",
            "member_state": "VALID",
            "gap_status": "NO GAP",
            "connect_identifier": "reportdb",
            "endpoint_unresolved": True,
            "direct_probe": direct_probe,
        }
        profile = {
            "architecture_type": "data_guard",
            "role_type": "primary",
            "topology_group": "oracle:data_guard:mesdb:reportdb",
            "cluster_name": "mesdb",
            "member_key": "mesdb",
            "db_unique_name": "mesdb",
            # Duplicate source observations exercise both the creation and
            # existing-peer merge paths without creating duplicate assets.
            "members": [member, dict(member)],
        }
        return build_internal_discovery(session, detected_profiles={seed.id: profile})
    finally:
        session.close()
        engine.dispose()


def _discovered_standby(bundle):
    return next(
        asset
        for asset in bundle.assets
        if asset.get("asset_type") == "database_instance"
        and (asset.get("attributes") or {}).get("native_type") == "discovered_database_instance"
    )


def test_unprobed_dg_member_keeps_identity_and_transport_evidence_without_claiming_health():
    standby = _discovered_standby(_discovery_bundle(direct_probe=False))
    attributes = standby["attributes"]

    assert standby["name"] == "reportdb"
    assert attributes["host"] == ""
    assert attributes["health_status"] == "unknown"
    assert attributes["discovery_status"] == "unverified"
    assert attributes["architecture_verified"] is False
    assert attributes["architecture_evidence"]["member_state"] == "VALID"
    assert attributes["architecture_evidence"]["gap_status"] == "NO GAP"
    assert attributes["architecture_evidence"]["connect_identifier"] == "reportdb"
    assert attributes["architecture_evidence"]["endpoint_unresolved"] is True


def test_directly_probed_dg_member_may_publish_observed_health():
    standby = _discovered_standby(_discovery_bundle(direct_probe=True))

    assert standby["name"] == "reportdb"
    assert standby["attributes"]["host"] == ""
    assert standby["attributes"]["health_status"] == "healthy"
    assert standby["attributes"]["discovery_status"] == "success"
    assert standby["attributes"]["architecture_verified"] is True
