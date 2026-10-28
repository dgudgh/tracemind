"""Restoring a template version must actually work - not end in TypeError.

Regressions covered here were found by reading policy_service and template_versions
against each other: the versioning module called update_policy() with keyword
arguments that function never accepted.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.inspection_policy import DBInspectionPolicy
from app.models.inspection_template_version import InspectionTemplateVersion
from app.services.inspection import template_versions


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    yield sessions
    engine.dispose()


def _make_policy(sessions, *, name="原模板", items=None):
    with sessions() as db:
        policy = DBInspectionPolicy(
            policy_key="synthetic-policy", name=name, enabled=1, read_only=0,
            db_types_json=["mysql"],
            item_config_json=items if items is not None else [
                {"key": "mysql.connection.used_pct", "enabled": True},
            ],
        )
        template_versions.add_policy(db, policy)
        template_versions.capture_version(db, policy, operation="create")
        db.commit()
        return policy.id


def test_restore_version_returns_policy_instead_of_crashing(db_factory):
    policy_id = _make_policy(db_factory)
    with db_factory() as db:
        policy = db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id == policy_id).one()
        policy.name = "改过的名字"
        policy.item_config_json = [{"key": "mysql.connection.used_pct", "enabled": False}]
        db.commit()

        result = template_versions.restore_version(db, policy_id, 1, expected_revision=1)

        assert result["name"] == "原模板", "restore must bring back the historical name"
        assert db.query(InspectionTemplateVersion).filter(
            InspectionTemplateVersion.policy_id == policy_id,
            InspectionTemplateVersion.operation == "restore",
        ).count() == 1, "restore itself is a versioned change and must be recorded"


def test_update_policy_version_snapshot_round_trip(db_factory):
    policy_id = _make_policy(db_factory)
    with db_factory() as db:
        policy = db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id == policy_id).one()

        template_versions.capture_version(db, policy, operation="update")
        db.commit()

        history = template_versions.list_versions(db, policy_id)
        assert [row["operation"] for row in history] == ["update", "create"]
        assert template_versions.get_version(db, policy_id, 1)["snapshot"]["policy_key"] == "synthetic-policy"


def test_edit_survives_missing_history_table(caplog):
    """History is optional infrastructure; editing a template is not.

    A deployment one migration behind must still be able to save a template.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DBInspectionPolicy.__table__])
    sessions = sessionmaker(bind=engine)
    try:
        from app.services.inspection import policy_service

        with sessions() as db:
            db.add(DBInspectionPolicy(policy_key="no-history", name="Before", enabled=0, read_only=0,
                                      db_types_json=["mysql"], item_config_json=[]))
            db.commit()

        with caplog.at_level("WARNING", logger="app.services.inspection.policy_service"):
            with sessions() as db:
                policy = db.query(DBInspectionPolicy).filter(DBInspectionPolicy.policy_key == "no-history").one()
                result = policy_service.update_policy(db, policy, {"name": "After"})

        assert result["name"] == "After", "the edit must be saved even with no version table"
        with sessions() as db:
            assert db.query(DBInspectionPolicy).one().name == "After"
        assert "version history unavailable" in caplog.text
    finally:
        engine.dispose()


def test_availability_probe_does_not_discard_pending_writes(db_factory):
    """`version_history_available` must not silently roll back the transaction.

    Probing through the engine opens a second connection; closing it rolled back
    the caller's uncommitted INSERT. The symptom was a StaleDataError at commit
    time - "expected to update 1 row, 0 matched" - because the ORM still believed
    the row was persistent. Any write path that probes mid-transaction is exposed.
    """
    with db_factory() as db:
        db.add(DBInspectionPolicy(
            policy_key="pending-write", name="未提交", enabled=1, read_only=0,
            db_types_json=["mysql"], item_config_json=[],
        ))
        db.flush()
        assert template_versions.version_history_available(db) is True
        survived = db.query(DBInspectionPolicy).filter(
            DBInspectionPolicy.policy_key == "pending-write"
        ).count()
        assert survived == 1, "the probe rolled back the pending insert"
        db.commit()
    with db_factory() as db:  # same engine via the fixture's StaticPool
        assert db.query(DBInspectionPolicy).filter(
            DBInspectionPolicy.policy_key == "pending-write"
        ).count() == 1


def test_create_clone_and_delete_all_leave_a_version_row(db_factory):
    """Every mutation path must be reconstructable, not just `update`.

    A history that starts at the first edit cannot answer "what did this template
    look like when it was created", and a clone without provenance loses the
    answer to "where did it come from".
    """
    from app.services.inspection import policy_service

    payload = {
        "policy_key": "lifecycle-policy",
        "name": "生命周期模板",
        "enabled": True,
        "db_types": ["mysql"],
        "item_config": [{"key": "mysql.connection.used_pct", "enabled": True}],
    }
    with db_factory() as db:
        policy_service.create_policy(db, dict(payload))
        created = db.query(DBInspectionPolicy).filter(
            DBInspectionPolicy.policy_key == "lifecycle-policy"
        ).one()

        versions = template_versions.list_versions(db, created.id)
        assert [row["operation"] for row in versions] == ["create"], versions

        clone_row = policy_service.clone_policy(db, created, "lifecycle-clone", "克隆模板")
        clone = db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id == clone_row["id"]).one()
        clone_versions = template_versions.list_versions(db, clone.id)
        assert [row["operation"] for row in clone_versions] == ["clone"], clone_versions
        assert clone_versions[0]["source_policy_id"] == created.id, "clone lost its provenance"
        assert clone_versions[0]["source_revision"] == 1, "clone did not name the revision it copied"

        policy_service.delete_policy(db, created)
        assert db.query(DBInspectionPolicy).filter(DBInspectionPolicy.id == created.id).count() == 0
        # list_versions is newest-first; assert against the revision number rather
        # than the returned order so the test survives a presentation change.
        tombstone = template_versions.list_versions(db, created.id)
        by_revision = {row["revision"]: row["operation"] for row in tombstone}
        assert by_revision == {1: "create", 2: "delete"}, tombstone
