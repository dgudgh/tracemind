import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.database import Base, initialize_schema
from app.migrations import runner
from app.migrations.initialization import initialize_schema as initialize_metadata
from app.migrations.policy import SchemaUpgradeRequired
from app.migrations.versions import (
    v20260908_02_inspection_jobs as jobs,
    v20260908_03_inspection_template_versions as versions,
)
from app.models import DBInspectionJob, InspectionTemplateVersion


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    value = create_engine(f"sqlite:///{tmp_path / 'registration.db'}")
    yield value
    value.dispose()


def test_inspection_models_and_version_scripts_are_registered():
    for model in (DBInspectionJob, InspectionTemplateVersion):
        assert Base.metadata.tables[model.__tablename__] is model.__table__
    assert jobs in runner.MIGRATIONS
    assert versions in runner.MIGRATIONS


def test_existing_installation_requires_explicit_upgrade_then_retains_history(engine, monkeypatch):
    current = runner.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(runner, "MIGRATIONS", tuple(m for m in current if m not in (jobs, versions)))
        initialize_metadata(engine, MetaData(), allow_upgrade=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE customer_sentinel (id INTEGER PRIMARY KEY, value TEXT)"))
        connection.execute(text("INSERT INTO customer_sentinel VALUES (1, 'retain')"))
    with pytest.raises(SchemaUpgradeRequired):
        initialize_metadata(engine, MetaData(), allow_upgrade=False)
    assert not inspect(engine).has_table("db_inspection_job")
    assert initialize_metadata(engine, MetaData(), allow_upgrade=True) == [jobs.REVISION, versions.REVISION]
    assert initialize_metadata(engine, MetaData(), allow_upgrade=False) == []
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT value FROM customer_sentinel")) == "retain"
    assert inspect(engine).has_table("db_inspection_job")
    assert inspect(engine).has_table("inspection_template_version")


def test_fresh_install_creates_immutable_history_and_refuses_destructive_downgrade(engine):
    initialize_schema(engine, allow_upgrade=True)
    with engine.begin() as connection:
        connection.execute(InspectionTemplateVersion.__table__.insert().values(
            policy_id=1, policy_key="test", revision=1, content_hash="a" * 64,
            snapshot_json={"name": "original"}, operation="create",
        ))
    for statement in (
        "UPDATE inspection_template_version SET policy_key = 'changed'",
        "DELETE FROM inspection_template_version",
    ):
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="retained"):
        runner.downgrade_revision(engine, versions.REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM inspection_template_version")) == 1
        assert versions.REVISION in runner._applied_revisions(connection)
    assert initialize_schema(engine, allow_upgrade=False) == []
