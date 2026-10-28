from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import normalized_service
from app.cmdb.models import (
    AssetCluster,
    AssetDiscoveryBatch,
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
    AssetDiscoveryBatch,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in MODELS:
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def discovered_peer(asset_id: int, *, host: str = "", hostname: str = "") -> dict:
    return {
        "id": asset_id,
        "asset_type": "database_instance",
        "name": hostname or f"peer-{asset_id}",
        "status": "active",
        "health_status": "unknown",
        "source_kind": "automatic",
        "attributes": {
            "native_instance_id": None,
            "native_type": "discovered_database_instance",
            "db_type": "mysql",
            "host": host,
            "hostname": hostname,
            "port": 3306,
            "architecture_type": "replication",
            "role_type": "replica",
            "topology_group": "mysql:replication:primary-uuid",
            "cluster_name": "orch-mysql",
        },
    }


def sync(db, inventory: list[dict]) -> None:
    normalized_service.sync_from_inventory(
        db,
        inventory,
        source_id=1,
        source_name="TraceMind 自动发现",
        source_type="internal",
        record_batch=False,
    )


def test_two_unresolved_members_get_distinct_hosts_without_fake_endpoints(db):
    sync(db, [discovered_peer(10), discovered_peer(11)])

    host_rows = db.query(AssetHost).order_by(AssetHost.id.asc()).all()
    instance_rows = db.query(AssetInstance).order_by(AssetInstance.source_asset_id.asc()).all()
    payloads = normalized_service.list_instances(db)

    assert len(host_rows) == 2
    assert {row.stable_key for row in host_rows} == {"legacy-host:10", "legacy-host:11"}
    assert {row.ip for row in host_rows} == {""}
    assert len({row.host_id for row in instance_rows}) == 2
    assert {row.ip for row in instance_rows} == {""}
    assert {item["endpoint"] for item in payloads} == {""}
    assert {item["ip"] for item in payloads} == {""}
    assert {item["host_ip"] for item in payloads} == {""}


def test_unresolved_member_moves_to_resolved_host_and_retires_placeholder(db):
    sync(db, [discovered_peer(10)])
    instance = db.query(AssetInstance).one()
    placeholder = db.get(AssetHost, instance.host_id)
    instance_id = instance.id
    placeholder_id = placeholder.id

    instance.name = "人工副本别名"
    instance.manual_fields = ["name"]
    placeholder.os_type = "人工确认的 Linux"
    placeholder.manual_fields = ["os_type"]
    db.commit()

    sync(db, [discovered_peer(10, host="192.0.2.11", hostname="orch-mysql02")])

    instance = db.get(AssetInstance, instance_id)
    placeholder = db.get(AssetHost, placeholder_id)
    resolved = db.get(AssetHost, instance.host_id)
    payload = normalized_service.list_instances(db)[0]

    assert instance.name == "人工副本别名"
    assert instance.manual_fields == ["name"]
    assert instance.ip == "192.0.2.11"
    assert instance.host_id != placeholder_id
    assert payload["endpoint"] == "192.0.2.11:3306"
    assert placeholder.status == "inactive"
    assert placeholder.os_type == "人工确认的 Linux"
    assert placeholder.manual_fields == ["os_type"]
    assert resolved.ip == "192.0.2.11"
    assert resolved.status != "inactive"


def test_serialization_hides_legacy_synthetic_host_addresses(db):
    sync(db, [discovered_peer(10)])
    instance = db.query(AssetInstance).one()
    host = db.get(AssetHost, instance.host_id)

    instance.ip = "主机-10"
    instance.attributes = {
        key: value
        for key, value in (instance.attributes or {}).items()
        if key != "endpoint_resolved"
    }
    host.ip = "legacy-host:10"
    host.attributes = {}
    db.commit()

    instance_payload = normalized_service.list_instances(db)[0]
    host_payload = normalized_service.list_hosts(db)[0]

    assert instance_payload["ip"] == ""
    assert instance_payload["host_ip"] == ""
    assert instance_payload["endpoint"] == ""
    assert host_payload["ip"] == ""


def test_hostname_endpoint_remains_valid(db):
    sync(db, [discovered_peer(10, host="orch-mysql02", hostname="orch-mysql02")])

    instance = db.query(AssetInstance).one()
    payload = normalized_service.list_instances(db)[0]

    assert instance.ip == "orch-mysql02"
    assert payload["ip"] == "orch-mysql02"
    assert payload["endpoint"] == "orch-mysql02:3306"

