"""T05a contract: managed-hot processes can verify but never execute DDL."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401
from app.core.database import Base, initialize_schema
from app.migrations.contract import migration_status, verify_schema_ready
from app.migrations.policy import SchemaUpgradeRequired


def test_fresh_database_is_not_ready_for_managed_hot(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        with pytest.raises(SchemaUpgradeRequired):
            verify_schema_ready(engine)
        assert not inspect(engine).has_table("db_instance")
    finally:
        engine.dispose()


def test_contract_becomes_ready_only_after_initialization(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ready.db'}")
    try:
        before = migration_status(engine)
        assert before["ready"] is False and before["pending"]
        initialize_schema(engine)
        after = migration_status(engine)
        assert after["ready"] is True and after["pending"] == []
        verified = verify_schema_ready(engine)
        assert verified["checksum"] == after["checksum"]
    finally:
        engine.dispose()
