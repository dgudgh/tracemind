"""C04 on a real MySQL control database: precision, row locks and true isolation.

The SQLite suites simulate both "another session revoked" and "the row is
locked". They cannot prove that MySQL's DATETIME truncation, REPEATABLE READ
snapshots and InnoDB row locks behave the way the fence assumes. These tests
run only when ``MESDBTOOLS_MYSQL_TEST_URL`` points at a disposable database;
they never touch a configured or production database.

    MESDBTOOLS_MYSQL_TEST_URL=mysql+pymysql://root@127.0.0.1:33306/mesdbtools_c04
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.core.transaction_lock import lock_transaction
from app.models.access_control import SecurityAuditEvent, UserAccessRole
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanStep
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import access_control, capability_registry as registry, plan_governance as governance, plan_service as plans

URL = os.environ.get("MESDBTOOLS_MYSQL_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set MESDBTOOLS_MYSQL_TEST_URL to a disposable MySQL database")

ADMIN, READER, OTHER = {"user_id": 1}, {"user_id": 2}, {"user_id": 3}


@pytest.fixture()
def engine():
    eng = create_engine(URL, pool_pre_ping=True, pool_size=8, max_overflow=8)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    with Session(eng, autoflush=False) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="mysql-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="mysql-reader", password_hash="unused", role="user", status="active"),
            User(id=3, username="mysql-other", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"].id),
            UserAccessRole(user_id=3, role_id=roles["viewer"].id),
        ])
        for identity in (10, 20):
            session.add(DBInstance(id=identity, instance_name=f"mysql-{identity}", db_type="mysql", host="127.0.0.1",
                                   port=3306, username="unused", password_cipher="env://SYNTHETIC_MYSQL_PASSWORD", enabled=1))
            session.add(AssetInstance(id=identity, native_instance_id=identity, stable_key=f"mysql:{identity}",
                                      name=f"mysql-{identity}", db_type="mysql", ip="127.0.0.1", port=3306,
                                      host_id=identity, region_id=1, site_id=1, zone_id=1, env_id=1, status="active"))
        session.commit()
        registry.sync_definitions(session, actor=ADMIN)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    with Session(engine, autoflush=False) as session:
        yield session


def _create(db, **kwargs):
    return plans.create_plan(db, actor=READER, capability_id="inspection.run.query",
                             definition_version=registry._entry("inspection.run.query")["version"], **kwargs)


def _pending(db, **kwargs):
    saved = _create(db, request_approval=True, **kwargs)
    governance.request_approval(db, saved["plan_id"], actor=READER, expected_revision=1)
    return saved


def _approve(session, saved, actor=ADMIN, **kwargs):
    return governance.approve_plan(session, saved["plan_id"], actor=actor, expected_revision=1,
                                   expected_binding_hash=saved["binding_hash"], **kwargs)


def test_approval_precision_survives_a_real_mysql_readback(engine, db, monkeypatch):
    instant = datetime(2026, 10, 20, 10, 0, 0, 987654)
    monkeypatch.setattr(plans, "_now", lambda: instant)
    saved = _pending(db, valid_for_seconds=60)
    _approve(db, saved)
    db.expire_all()
    with Session(engine, autoflush=False) as verify:
        plan = verify.get(DBCapabilityPlan, saved["plan_id"])
        assert plan.approved_at == datetime(2026, 10, 20, 10, 0, 0)
        assert plan.approval_expires_at == datetime(2026, 10, 20, 10, 1, 0)
        # The audited JSON must equal the stored column, not the pre-truncation value.
        decision = verify.query(SecurityAuditEvent).filter_by(action="approve_plan").one()
        assert decision.details["approved_at"] == plan.approved_at.isoformat() + "Z"
        assert decision.details["expires_at"] == plan.approval_expires_at.isoformat() + "Z"
        assert governance.approval_problem(verify, plan) is None
        done = plans.execute_plan(verify, saved["plan_id"], actor=READER)
        assert done["outcome"] == "succeeded"


def test_second_approval_waits_on_the_real_row_lock(engine, db):
    saved = _pending(db)
    holder = Session(engine, autoflush=False)
    lock_transaction(holder, "capability_plan_state")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_approve_in_new_session, engine, saved)
        time.sleep(0.6)
        assert not future.done(), "the second approver did not wait for the InnoDB row lock"
        holder.commit()
        result = future.result(timeout=20)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.5, f"unexpectedly fast lock acquisition: {elapsed:.2f}s"
    assert result["status"] == "approved"
    holder.close()


def _approve_in_new_session(engine, saved):
    with Session(engine, autoflush=False) as other:
        return _approve(other, saved)


def test_revoke_in_another_mysql_session_during_handler_never_publishes(engine, db, monkeypatch):
    saved = _pending(db)
    _approve(db, saved)
    original = registry.execute_read

    def revoke_while_reading(*args, **kwargs):
        result = original(*args, **kwargs)
        with Session(engine, autoflush=False) as other:
            governance.revoke_approval(other, saved["plan_id"], actor=ADMIN, expected_revision=1)
        return result

    monkeypatch.setattr(registry, "execute_read", revoke_while_reading)
    with pytest.raises(plans.PlanOwnershipLost, match="approval_revoked"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    with Session(engine, autoflush=False) as verify:
        assert verify.get(DBCapabilityPlan, saved["plan_id"]).outcome is None
        assert verify.query(DBCapabilityPlanStep).filter_by(plan_id=saved["plan_id"], result_ref_json=None).count() == 1


def test_lock_wait_timeout_cannot_leave_a_half_approval(engine, db):
    saved = _pending(db)
    holder = Session(engine, autoflush=False)
    lock_transaction(holder, "capability_plan_state")
    with Session(engine, autoflush=False) as blocked:
        blocked.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        with pytest.raises(OperationalError) as excinfo:
            _approve(blocked, saved)
        assert "1205" in str(excinfo.value.orig), str(excinfo.value.orig)
        blocked.rollback()
    holder.rollback()
    holder.close()
    with Session(engine, autoflush=False) as verify:
        plan = verify.get(DBCapabilityPlan, saved["plan_id"])
        assert plan.approval_state == "pending" and plan.approved_by is None
        assert verify.query(SecurityAuditEvent).filter_by(action="approve_plan").count() == 0
        # A retry after the lock is gone succeeds; the timeout was not a decision.
        _approve(verify, saved)
        assert verify.get(DBCapabilityPlan, saved["plan_id"]).approval_state == "approved"


def test_two_simultaneous_approvals_yield_one_decision(engine, db):
    saved = _pending(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(_approve_in_new_session, engine, saved) for _ in range(2)]
    outcomes = []
    for future in results:
        try:
            outcomes.append(("ok", future.result(timeout=20)["status"]))
        except plans.PlanError as exc:
            outcomes.append(("error", exc.code))
    assert outcomes.count(("ok", "approved")) == 1, outcomes
    assert sum(1 for kind, _ in outcomes if kind == "error") == 1, outcomes
    with Session(engine, autoflush=False) as verify:
        assert verify.query(SecurityAuditEvent).filter_by(action="approve_plan").count() == 1
