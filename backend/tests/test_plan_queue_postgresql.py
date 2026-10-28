"""C06 on a real PostgreSQL control database: batching, claiming and lease recovery.

SQLite can fake a lot of queue behaviour, but not the things that break in production:
DATETIME truncation of deadlines/leases, PostgreSQL row locks around the claim, and a
second process seeing the first process's committed state. Only one worker may ever
own an execution, and an expired owner must become "needs reconciliation" rather
than being silently replayed.

    MESDBTOOLS_PG_TEST_URL=postgresql+psycopg://postgres@/postgres?host=/tmp/mesdbtools-pgdata2
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import access_control, capability_registry as registry, plan_service as plans
from app.services import plan_queue as queue
from app.tasks import plan_worker

URL = os.environ.get("MESDBTOOLS_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set MESDBTOOLS_PG_TEST_URL to a disposable PostgreSQL database")

ADMIN, READER = {"user_id": 1}, {"user_id": 2}


@pytest.fixture()
def engine():
    eng = create_engine(URL, pool_pre_ping=True, pool_size=8, max_overflow=8)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    with Session(eng, autoflush=False) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="queue-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="queue-reader", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"].id),
        ])
        for identity in (10, 20):
            session.add(DBInstance(id=identity, instance_name=f"pgq-{identity}", db_type="mysql", host="127.0.0.1",
                                   port=3306, username="unused", password_cipher="env://SYNTHETIC_PG_QUEUE_PASSWORD", enabled=1))
            session.add(AssetInstance(id=identity, native_instance_id=identity, stable_key=f"pgq:{identity}",
                                      name=f"pgq-{identity}", db_type="mysql", ip="127.0.0.1", port=3306,
                                      host_id=identity, region_id=1, site_id=1, zone_id=1, env_id=1, status="active"))
        session.commit()
        registry.sync_definitions(session, actor=ADMIN)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    with Session(engine, autoflush=False) as session:
        yield session


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)


def _plan(db, instance_id=10):
    return plans.create_plan(
        db, actor=READER, capability_id="inspection.run.query",
        definition_version=registry._entry("inspection.run.query")["version"],
        instance_id=instance_id,
    )


def _enqueue(db, plan_ids=None, key="mysql-batch"):
    ids = plan_ids or [_plan(db)["plan_id"]]
    return queue.enqueue_batch(db, ids, actor=READER, idempotency_key=key)


def test_acceptance_replay_and_readback_survive_a_new_mysql_session(db):
    batch = _enqueue(db)
    assert batch["accepted"] and batch["status"] == "queued"
    batch_id = batch["batch_id"]
    db.close()
    with Session(db.get_bind(), autoflush=False) as fresh:
        replay = _enqueue(fresh, [batch["items"][0]["plan_id"]])
        assert replay["reused"] and replay["batch_id"] == batch_id
        read = queue.read_batch(fresh, batch_id, actor=READER)
        assert read["items"] == batch["items"]
        stored = fresh.get(DBCapabilityBatch, batch_id)
        assert stored.deadline_at > stored.created_at
        # MySQL DATETIME keeps whole seconds; the deadline must still be in the future
        # after the round trip instead of being truncated into a pre-expired batch.
        assert stored.deadline_at > queue._now()
        assert fresh.query(DBCapabilityBatch).count() == 1


def test_two_real_workers_only_one_owns_the_execution(db, factory):
    batch = _enqueue(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(plan_worker.process_one, factory, worker_id=f"w{index}") for index in range(2)]
    outcomes = [future.result(timeout=30) for future in results]
    assert sum(1 for item in outcomes if item is not None) == 1, outcomes
    with Session(db.get_bind(), autoflush=False) as verify:
        execution = verify.query(DBCapabilityExecution).filter_by(batch_id=batch["batch_id"]).one()
        assert execution.state not in {"queued", "running"}
        assert execution.owner_token is None
        # A terminal claim must be backed by real persisted step evidence, not the envelope.
        plan = verify.get(DBCapabilityPlan, execution.plan_id)
        assert plan.outcome is not None


def test_expired_owner_after_start_requires_reconciliation(db, factory):
    batch = _enqueue(db)
    claim = queue._claim(factory, "w-original")
    assert claim is not None
    with factory() as fresh:
        # The worker has actually begun: a claim plus a running plan is what makes
        # recovery unsafe to replay. A never-started claim is simply requeued.
        plan = fresh.get(DBCapabilityPlan, claim["plan_id"])
        plan.status, plan.started_at = "running", queue._now()
        fresh.query(DBCapabilityExecution).filter_by(id=claim["id"]).update(
            {"lease_expires_at": queue._now() - timedelta(seconds=1)})
        fresh.commit()
    assert queue._claim(factory, "w-next") is None
    with factory() as verify:
        execution = verify.query(DBCapabilityExecution).filter_by(batch_id=batch["batch_id"]).one()
        assert execution.state == "reconciliation_required"
        assert execution.reason == "deadline_or_owner_lost_after_start"
        assert execution.owner_token is None
        assert verify.get(DBCapabilityPlan, execution.plan_id).outcome is None


def test_cancel_before_start_is_delivery_cancel_not_a_fake_run(db, factory):
    batch = _enqueue(db)
    cancelled = queue.cancel_batch(db, batch["batch_id"], actor=READER)
    assert cancelled["status"] == "settled" and cancelled["outcome"] == "cancelled"
    assert queue._claim(factory, "w-after-cancel") is None
    with factory() as verify:
        execution = verify.query(DBCapabilityExecution).filter_by(batch_id=batch["batch_id"]).one()
        assert execution.state == "cancelled"
        assert execution.reason == "delivery_cancelled_before_start"
        plan = verify.get(DBCapabilityPlan, execution.plan_id)
        assert plan.status != "settled" and plan.outcome is None
