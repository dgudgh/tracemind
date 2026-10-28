import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import normalized_service as service
from app.cmdb.models import (AssetRegion, AssetSite, AssetZone, AssetEnvironment, AssetHost,
                            AssetCluster, AssetInstance, AssetRelation, AssetDiscoveryBatch)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (AssetRegion, AssetSite, AssetZone, AssetEnvironment, AssetHost,
                  AssetCluster, AssetInstance, AssetRelation, AssetDiscoveryBatch):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def asset(number=1, **attributes):
    return {"id": number, "asset_type": "database_instance", "name": f"node-{number}",
            "status": "active", "health_status": "unknown", "source_kind": "automatic",
            "attributes": {"db_type": "mysql", "host": f"192.0.2.{number}", "port": 3306,
                           "architecture_type": "standalone", **attributes}}


def sync(db, inventory):
    return service.sync_from_inventory(db, inventory, source_id=1, source_name="synthetic",
                                       source_type="internal", record_batch=False)


def legacy(db):
    r = AssetRegion(code=f"region-{service._key('华东一区')}", name="华东一区")
    db.add(r); db.flush()
    s = AssetSite(region_id=r.id, code="site-idc", name="上海 IDC")
    db.add(s); db.flush()
    z = AssetZone(region_id=r.id, site_id=s.id, code="zone-a", name="A 区")
    db.add(z); db.flush()
    e = AssetEnvironment(region_id=r.id, site_id=s.id, zone_id=z.id,
                         code="mes-production", name="MES 生产环境", env_type="production")
    db.add(e); db.flush()
    return r, s, z, e


def test_missing_locations_are_explicit_unknown_and_idempotent(db):
    records = [asset(1), asset(2)]
    sync(db, records); sync(db, records)
    assert [db.query(m).count() for m in (AssetRegion, AssetSite, AssetZone, AssetEnvironment)] == [1, 1, 1, 1]
    instances = service.list_instances(db)
    assert all([item[key] for key in ("region", "site", "zone", "environment")] == ["未配置"] * 4 for item in instances)
    summary = service.overview(db)
    assert summary["configured_region_count"] == summary["configured_environment_count"] == 0
    assert summary["region_distribution"] == [{"name": "未配置", "count": 2}]
    assert summary["environment_distribution"] == [{"name": "未配置", "count": 2}]
    assert service.asset_tree(db)["regions"][0]["name"] == "未配置"
    assert db.query(AssetEnvironment).one().env_type == "unknown"


@pytest.mark.parametrize("name,kind", [("生产", "production"), ("production", "production"),
    ("测试", "test"), ("preprod", "staging"), ("预生产", "staging"),
    ("开发", "development"), ("product-demo", "custom")])
def test_explicit_environment_keeps_its_name_without_mes_prefix(db, name, kind):
    env = service._ensure_location(db, {"environment": name})[3]
    assert env.name == name and env.env_type == kind


@pytest.mark.parametrize("first,second", [("生产", "production"),
    ("MES 生产环境", "MES 测试环境"), ("生产1", "测试1"), ("prod+a", "prod a")])
def test_different_explicit_environment_names_do_not_merge(db, first, second):
    a = service._ensure_location(db, {"environment": first})[3]
    b = service._ensure_location(db, {"environment": second})[3]
    assert a.id != b.id and a.name == first and b.name == second


def test_explicit_source_locations_are_preserved_in_projection(db):
    sync(db, [asset(region_name="客户区域", site_name="客户机房", zone_name="客户分区", environment="客户环境")])
    row = service.list_instances(db)[0]
    assert [row[key] for key in ("region", "site", "zone", "environment")] == ["客户区域", "客户机房", "客户分区", "客户环境"]


def test_manual_location_quartet_is_unchanged_after_resync(db):
    sync(db, [asset()])
    configured = service._ensure_location(db, {"region": "人工区域", "site": "人工机房", "zone": "人工可用区", "environment": "人工环境"})
    ids = dict(zip(("region_id", "site_id", "zone_id", "env_id"), [row.id for row in configured]))
    for model in (AssetHost, AssetCluster, AssetInstance):
        row = db.query(model).one()
        for key, value in ids.items():setattr(row, key, value)
        row.manual_fields = list(ids)
    db.commit();sync(db, [asset()])
    for model in (AssetHost, AssetCluster, AssetInstance):
        assert {key: getattr(db.query(model).one(), key) for key in ids} == ids


def test_unused_automatic_legacy_defaults_are_retired_without_deleting_history(db):
    old = legacy(db)
    sync(db, [asset()])
    assert all(row.status == "inactive" for row in old)
    assert all(db.get(type(row), row.id) is not None for row in old)


def test_explicit_legacy_name_and_manual_children_protect_old_locations(db):
    r, s, z, e = legacy(db)
    e.source_type = "manual"
    sync(db, [asset()])
    assert all(row.status == "active" for row in (r, s, z, e))
    e.source_type = "automatic"
    service._retire_unused_legacy_location_defaults(db, [{"attributes": {"region": "华东一区"}}])
    assert r.status == "active"


def test_inactive_location_becomes_active_when_source_explicitly_uses_it(db):
    old = legacy(db)
    for row in old:row.status = "inactive"
    actual = service._ensure_location(db, {"region": "华东一区", "site": "上海 IDC", "zone": "A 区"})
    assert all(row.status == "active" for row in actual)
