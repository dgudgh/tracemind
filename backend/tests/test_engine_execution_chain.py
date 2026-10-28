"""Ordinary plan -> durable queue -> exact signed native UDS Provider -> core readback."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.db_instance import DBInstance
from app.models.engine_execution import DBEnginePlanBinding, DBEngineResultReceipt
from app.models.user import User
from app.runtime.engine_host.store import FixtureStore, Package
from app.runtime.engine_packs.archive import build_archive
from app.runtime.engine_packs.repository import LabRepository
from app.services import access_control, capability_registry as registry, engine_execution, plan_queue, plan_service
from test_engine_provider_rpc import _setup

ADMIN = {"user_id": 1}
READER = {"user_id": 2}


@pytest.fixture
def chain(tmp_path):
    root, package, broker, supervisor = _setup()
    repo = LabRepository(tmp_path / "repository", trusted_keys=package.trusted_keys)
    archive = tmp_path / "native.tmengine"
    receipt = build_archive(package.source, package.manifest, package.signature, archive,
                            trusted_keys=package.trusted_keys, expected_namespace="tmengine.test",
                            expected_channel="test")
    (repo.root / "incoming" / "native.tmengine").write_bytes(archive.read_bytes())
    repo.install("native.tmengine", "install-1", expected_archive_sha256=receipt["archive_sha256"])
    supervisor.start(package.source, package.manifest, package.signature, package.trusted_keys,
                     expected_namespace="tmengine.test", expected_channel="test")
    store = FixtureStore(tmp_path / "store.sqlite")
    store.bootstrap_active(Package("mysql", package.digest, package.manifest["version"], state="active",
                                   payload_digest=package.payload_digest,
                                   runtime_digest=package.manifest["runtime_digest"],
                                   sdk_version=package.manifest["sdk_version"],
                                   task_schema=package.manifest["task_schema"],
                                   result_schema=package.manifest["result_schema"]))
    manager = SimpleNamespace(store=store, repo=repo, _hosts={"mysql": supervisor}, _retained_hosts={})
    bridge = engine_execution.EngineExecutionBridge(manager=manager, core_release="platform-test-P")
    engine_execution.install_bridge(bridge)
    db_engine = create_engine(f"sqlite:///{tmp_path / 'platform.sqlite'}",
                              connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine, autoflush=False)
    with factory() as db:
        roles = {row.role_key: row.id for row in access_control.ensure_builtin_roles(db)}
        db.add_all([User(id=1, username="engine-admin", password_hash="unused", role="admin", status="active"),
                    User(id=2, username="engine-reader", password_hash="unused", role="user", status="active"),
                    UserAccessRole(user_id=2, role_id=roles["viewer"]),
                    UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"),
                    DBInstance(id=10, instance_name="fixture", db_type="mysql", host="fixture-target",
                               port=3306, username="fixture-user", password_cipher="env://TEST_ONLY_SECRET_VALUE",
                               enabled=1),
                    AssetInstance(id=10, native_instance_id=10, stable_key="fixture:10", name="fixture",
                                  db_type="mysql", ip="fixture-target", port=3306, host_id=10,
                                  region_id=1, site_id=1, zone_id=1, env_id=1, status="active")])
        db.commit()
        registry.sync_definitions(db, actor=ADMIN)
    try:
        yield factory, bridge, manager
    finally:
        engine_execution.install_bridge(None)
        supervisor.stop()
        broker.close()
        store.close()
        db_engine.dispose()


def test_pinned_queue_uses_real_provider_and_core_readback(chain):
    factory, bridge, manager = chain
    with factory() as db:
        plan = plan_service.create_plan(db, actor=READER, capability_id="operations.session.read",
                                        definition_version=2, instance_id=10, params={}, idempotency_key="sessions-1")
        assert plan["status"] == "validated"
        frozen = db.get(DBEnginePlanBinding, plan["plan_id"])
        assert frozen.engine_id == "mysql" and frozen.operation == "sessions"
        identity = dict(frozen.execution_identity_json)
        batch = plan_queue.enqueue_batch(db, [plan["plan_id"]], actor=READER,
                                         idempotency_key="batch-1", schema_version=2)
        assert batch["status"] == "queued"
    assert plan_queue.process_one(factory, worker_id="wrong", worker_identity={
        **identity, "core_release": "platform-test-P+1"}) is None
    assert plan_queue.process_one(factory, worker_id="legacy") is None
    result = plan_queue.process_one(factory, worker_id="matched", worker_identity=identity)
    with factory() as db:
        detail = plan_service.read_plan(db, plan["plan_id"], actor=READER)
    assert result["queue_state"] == "archived", (result, detail["outcome"], detail["steps"][0]["outcome_kind"])
    with factory() as db:
        read = plan_service.read_plan(db, plan["plan_id"], actor=READER)
        assert read["outcome"] == "succeeded"
        assert read["engine_result"]["items"][0]["result"]["values"]["rows"] == [{"session_count": 2}]
        assert db.query(DBEngineResultReceipt).filter_by(plan_id=plan["plan_id"]).count() == 1
        assert plan_queue.read_batch(db, batch["batch_id"], actor=READER, schema_version=2)["status"] == "settled"
    # Core readback is independent of an installed/running Provider.
    manager._hosts.clear()
    with factory() as db:
        assert plan_service.read_plan(db, plan["plan_id"], actor=READER)["engine_result"]["items"]


def test_revocation_before_claim_blocks_pinned_task(chain):
    factory, bridge, manager = chain
    with factory() as db:
        plan = plan_service.create_plan(db, actor=READER, capability_id="operations.session.read",
                                        definition_version=2, instance_id=10, params={}, idempotency_key="sessions-revoke")
        plan_queue.enqueue_batch(db, [plan["plan_id"]], actor=READER,
                                 idempotency_key="batch-revoke", schema_version=2)
        frozen = db.get(DBEnginePlanBinding, plan["plan_id"])
        identity = dict(frozen.execution_identity_json)
    with factory() as db:
        user = db.get(User, 2)
        user.status = "locked"
        db.commit()
    result = plan_queue.process_one(factory, worker_id="matched", worker_identity=identity)
    assert result is None
    with factory() as db:
        from app.models.capability_execution import DBCapabilityExecution
        member = db.query(DBCapabilityExecution).filter_by(plan_id=plan["plan_id"]).one()
        assert member.state == "blocked"


def test_direct_execution_and_disabled_new_admission_refused(chain):
    factory, bridge, manager = chain
    with factory() as db:
        plan = plan_service.create_plan(db, actor=ADMIN, capability_id="operations.session.read",
                                        definition_version=2, instance_id=10, params={}, idempotency_key="direct")
        with pytest.raises(plan_service.PlanError, match="pinned_worker_required"):
            plan_service.execute_plan(db, plan["plan_id"], actor=ADMIN)
        db.rollback()
    manager.store.set_package_state("mysql", manager.store.active("mysql").digest, "disabled")
    with factory() as db:
        with pytest.raises(plan_service.PlanError, match="provider_disabled"):
            plan_service.create_plan(db, actor=ADMIN, capability_id="operations.session.read",
                                     definition_version=2, instance_id=10, params={}, idempotency_key="disabled")


def test_legacy_pending_backfill_requires_fresh_prepare(chain):
    from scripts.engine_binding_backfill import backfill
    from app.models.engine_execution import DBEngineInstanceBinding
    from app.models.capability_execution import DBCapabilityExecution
    factory, bridge, manager = chain
    engine_execution.install_bridge(None)
    with factory() as db:
        legacy = plan_service.create_plan(db, actor=ADMIN, capability_id="operations.session.read",
                                          definition_version=2, instance_id=10, params={}, idempotency_key="legacy")
        plan_queue.enqueue_batch(db, [legacy["plan_id"]], actor=ADMIN,
                                 idempotency_key="legacy-batch", schema_version=2)
        assert backfill(db) == {"mapped": 1, "unresolved": 0, "conflict": 0, "reprepare_required": 1}
        db.commit()
        assert backfill(db) == {"mapped": 0, "unresolved": 0, "conflict": 0, "reprepare_required": 0}
        assert db.get(DBEngineInstanceBinding, 10).engine_id == "mysql"
    engine_execution.install_bridge(bridge)
    assert plan_queue.process_one(factory, worker_id="old") is None
    with factory() as db:
        member = db.query(DBCapabilityExecution).filter_by(plan_id=legacy["plan_id"]).one()
        assert member.state == "blocked" and member.reason == "reprepare_required"
