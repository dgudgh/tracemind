"""C13 batch 1: the six read capabilities actually reach their existing service readers.

Each case proves three separate things: the registry resolves the reviewed handler name for
the instance's engine, the handler calls exactly one existing service function, and the
server never inherits a second execution path. Denial cases (engine, scope, params) run
before the service is touched.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import (
    access_control,
    archive_fra,
    basic_info,
    capability_handler,
    capability_registry as registry,
    generic_db_ops,
    mysql_basic,
    mysql_binlog,
    mysql_capacity,
    mysql_lock,
    mysql_slow_sql,
    slow_sql_analysis,
    tablespace_enhanced,
)
from app.services.capability_declarations import CAPABILITY_DECLARATIONS


BY_ID = {item["capability_id"]: item for item in CAPABILITY_DECLARATIONS}
MODULES = {
    "archive_fra": archive_fra,
    "basic_info": basic_info,
    "generic_db_ops": generic_db_ops,
    "mysql_basic": mysql_basic,
    "mysql_binlog": mysql_binlog,
    "mysql_capacity": mysql_capacity,
    "mysql_lock": mysql_lock,
    "mysql_slow_sql": mysql_slow_sql,
    "slow_sql_analysis": slow_sql_analysis,
    "tablespace_enhanced": tablespace_enhanced,
}
INSTANCE_IDS = {"mysql": 10, "oracle": 11, "postgresql": 12, "sqlserver": 13, "redis": 99}
ADMIN = {"user_id": 1}
RESTRICTED = {"user_id": 2}

CAPABILITIES = (
    "operations.runtime_overview.read",
    "operations.space.read",
    "operations.log.read",
    "operations.session.read",
    "performance.lock.read",
    "performance.slow_sql.read",
)
EXPECTED_HANDLER = {
    "operations.runtime_overview.read": "operations.runtime_overview",
    "operations.space.read": "operations.space",
    "operations.log.read": "operations.log",
    "operations.session.read": "operations.session",
    "performance.lock.read": "performance.lock",
    "performance.slow_sql.read": "performance.slow_sql",
}
# The one existing reader each engine is allowed to reach. `section` is the generic_db_ops
# section name for PostgreSQL/SQL Server; None means the service takes only the instance.
# `sends_list` marks services that return a bare list instead of a section payload.
EXECUTION_CASES = [
    ("operations.runtime_overview.read", "oracle", "basic_info", "get_basic_info", None, False),
    ("operations.runtime_overview.read", "mysql", "mysql_basic", "get_basic_info", None, False),
    ("operations.runtime_overview.read", "postgresql", "generic_db_ops", "collect_section", "overview", False),
    ("operations.runtime_overview.read", "sqlserver", "generic_db_ops", "collect_section", "overview", False),
    ("operations.space.read", "oracle", "tablespace_enhanced", "get_tablespace_overview", None, False),
    ("operations.space.read", "mysql", "mysql_capacity", "get_schema_sizes", None, True),
    ("operations.space.read", "postgresql", "generic_db_ops", "collect_section", "space", False),
    ("operations.space.read", "sqlserver", "generic_db_ops", "collect_section", "space", False),
    ("operations.log.read", "oracle", "archive_fra", "get_archive_config", None, False),
    ("operations.log.read", "mysql", "mysql_binlog", "get_binlog_overview", None, False),
    ("operations.log.read", "postgresql", "generic_db_ops", "collect_section", "wal", False),
    ("operations.log.read", "sqlserver", "generic_db_ops", "collect_section", "logs", False),
    ("operations.session.read", "mysql", "mysql_lock", "get_lock_transaction_center", None, False),
    ("operations.session.read", "postgresql", "generic_db_ops", "collect_section", "sessionTransaction", False),
    ("operations.session.read", "sqlserver", "generic_db_ops", "collect_section", "sessionTransaction", False),
    ("performance.lock.read", "postgresql", "generic_db_ops", "collect_section", "sessionTransaction", False),
    ("performance.lock.read", "sqlserver", "generic_db_ops", "collect_section", "sessionTransaction", False),
    ("performance.slow_sql.read", "oracle", "slow_sql_analysis", "get_slow_sql_list", None, False),
    ("performance.slow_sql.read", "mysql", "mysql_slow_sql", "get_slow_sql_center", None, False),
]
UNSUPPORTED_ENGINE = {
    "operations.runtime_overview.read": "redis",
    "operations.space.read": "redis",
    "operations.log.read": "redis",
    "operations.session.read": "oracle",
    "performance.lock.read": "redis",
    "performance.slow_sql.read": "postgresql",
}


class _Spy:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="batch1-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="batch1-scoped", password_hash="unused", role="user", status="active"),
        ])
        session.flush()
        session.add(UserAccessRole(user_id=2, role_id=roles["viewer"].id))
        for identity, db_type in ((10, "mysql"), (11, "oracle"), (12, "postgresql"),
                                  (13, "sqlserver"), (99, "redis")):
            session.add(DBInstance(id=identity, instance_name=f"{db_type}-{identity}", db_type=db_type,
                                   host="127.0.0.1", port=1000 + identity, username="unused", enabled=1,
                                   password_cipher="env://SYNTHETIC_CAPABILITY_TEST_PASSWORD"))
            session.add(AssetInstance(id=identity, native_instance_id=identity,
                                      stable_key=f"asset:{identity}", name=f"asset-{identity}",
                                      db_type=db_type, ip="127.0.0.1", port=1000 + identity,
                                      host_id=identity, region_id=1, site_id=1,
                                      zone_id=1, env_id=1, status="active"))
        # The restricted user may see exactly instance 10 (mysql); 11/12/13/99 stay outside.
        session.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"))
        session.commit()
        registry.sync_definitions(session, actor=ADMIN)
        yield session
    engine.dispose()


def _payload(key: str = "items"):
    return {"status": "ok", key: [{"n": 1}, {"n": 2}, {"n": 3}]}


@pytest.mark.parametrize(
    "capability_id,engine,module_key,function,section,sends_list", EXECUTION_CASES,
    ids=[f"{case[0]}-{case[1]}" for case in EXECUTION_CASES],
)
def test_supported_engine_calls_exactly_one_reviewed_reader(
        db, monkeypatch, capability_id, engine, module_key, function, section, sends_list):
    # Oracle's slow SQL reader calls its list "rows"; every other reader uses "items".
    item_key = "rows" if capability_id == "performance.slow_sql.read" and engine == "oracle" else "items"
    result = [{"n": 1}] if sends_list else _payload(item_key)
    spy = _Spy(result)
    monkeypatch.setattr(MODULES[module_key], function, spy)

    handler, context = registry.resolve_handler(
        db, capability_id, BY_ID[capability_id]["version"], actor=ADMIN,
        instance_id=INSTANCE_IDS[engine])
    assert handler is capability_handler.HANDLERS[EXPECTED_HANDLER[capability_id]]

    value = handler(context)

    assert len(spy.calls) == 1
    args, kwargs = spy.calls[0]
    assert args[0].id == INSTANCE_IDS[engine]
    if section:
        assert args[1] == section
    elif capability_id == "performance.slow_sql.read" and engine == "mysql":
        # The page window bounds the upstream LIMIT instead of fetching an unbounded set.
        assert args[1] == 50 and args[2] is None
    elif capability_id == "performance.slow_sql.read":
        assert args[1] == "top_avg" and kwargs == {"hours": 1}
    if sends_list:
        assert value["items"] == result
    else:
        assert value[item_key] == result[item_key]
    if capability_id in {"operations.session.read", "performance.lock.read",
                         "performance.slow_sql.read"}:
        assert value["page"] == 1 and value["page_size"] == 50
        assert value["total"] == len(result if sends_list else result[item_key])


def test_oracle_lock_reader_reuses_live_blocking_tree_and_pages_waiters(db, monkeypatch):
    from app.api.v1 import oracle as oracle_api

    result = {
        "tree": [{"sid": 11, "wait_s": 30}, {"sid": 12, "wait_s": 10}],
        "lock_pairs": [{"blocker_sid": 11, "waiter_sid": 12}],
        "summary": {"blocker_count": 1, "waiter_count": 1, "total": 2, "max_wait_seconds": 30},
    }
    spy = _Spy(result)
    monkeypatch.setattr(oracle_api, "_get_blocking_tree", spy)

    handler, context = registry.resolve_handler(
        db, "performance.lock.read", BY_ID["performance.lock.read"]["version"],
        actor=ADMIN, instance_id=INSTANCE_IDS["oracle"], params={"page": 2, "page_size": 1})
    value = handler(context)

    assert len(spy.calls) == 1 and spy.calls[0][0][0].id == INSTANCE_IDS["oracle"]
    assert spy.calls[0][1] == {"strict": True}
    assert value == {
        "items": [{"sid": 12, "wait_s": 10}],
        "lock_pairs": result["lock_pairs"],
        "summary": result["summary"],
        "total": 2,
        "page": 2,
        "page_size": 1,
    }


def test_mysql_lock_reader_preserves_live_center_status(db, monkeypatch):
    center = {
        "status": "ok",
        "sections": {"lock_waits": [{"waiting_thread_id": 10}]},
        "summary": {"lock_wait_count": 1},
        "warnings": [],
    }
    spy = _Spy(center)
    monkeypatch.setattr(mysql_lock, "get_lock_transaction_center", spy)
    handler, context = registry.resolve_handler(
        db, "performance.lock.read", BY_ID["performance.lock.read"]["version"],
        actor=ADMIN, instance_id=INSTANCE_IDS["mysql"])

    value = handler(context)

    assert len(spy.calls) == 1 and spy.calls[0][0][0].id == INSTANCE_IDS["mysql"]
    assert spy.calls[0][1] == {"strict_lock_waits": True}
    assert value["items"] == center["sections"]["lock_waits"]
    assert value["summary"] == center["summary"]
    assert value["total"] == 1


def test_mysql_lock_reader_refuses_an_error_center_instead_of_returning_zero(db, monkeypatch):
    monkeypatch.setattr(mysql_lock, "get_lock_transaction_center", _Spy({
        "status": "error", "error": "connection failed", "sections": {}, "items": [],
    }))
    handler, context = registry.resolve_handler(
        db, "performance.lock.read", BY_ID["performance.lock.read"]["version"],
        actor=ADMIN, instance_id=INSTANCE_IDS["mysql"])

    with pytest.raises(RuntimeError, match="mysql_lock_query_failed"):
        handler(context)


def test_mysql_strict_lock_query_distinguishes_a_real_empty_result_from_query_failure(monkeypatch):
    cursor = object()
    monkeypatch.setattr(mysql_lock, "_table_exists", lambda _cur, schema, table: schema == "performance_schema")
    monkeypatch.setattr(mysql_lock, "_safe_query", lambda *_args, **_kwargs: [])
    assert mysql_lock._fetch_lock_waits(cursor, [], strict=True) == []

    def failed_query(_cur, _sql, warnings, label, params=None):
        warnings.append(f"{label}采集失败")
        return []

    monkeypatch.setattr(mysql_lock, "_safe_query", failed_query)
    with pytest.raises(RuntimeError, match="mysql_lock_query_failed"):
        mysql_lock._fetch_lock_waits(cursor, [], strict=True)


def test_declarations_have_reviewed_versions_and_are_bound_to_these_handlers():
    for capability_id in CAPABILITIES:
        entry = BY_ID[capability_id]
        assert entry["version"] == (3 if capability_id == "performance.lock.read" else 2)
        assert entry["handler"] == EXPECTED_HANDLER[capability_id]
        assert entry["handler_status"] == "wired"
        assert entry["execution"] == "server"
        assert entry["kind"] == "read"
        assert entry["requires_instance"] is True


def test_handler_and_param_schema_tables_have_no_orphans():
    from app.services.capability_handler import HANDLERS, PARAM_SCHEMAS

    assert set(HANDLERS) == set(PARAM_SCHEMAS)
    assert {EXPECTED_HANDLER[key] for key in CAPABILITIES} <= set(HANDLERS)
    assert len(HANDLERS) >= 23  # batch 1 baseline; later C13/C14 batches extend it
    assert set(HANDLERS) == set(PARAM_SCHEMAS)


def test_rolling_a_definition_back_is_blocked_instead_of_executed(db, monkeypatch):
    """v2 rows already in the ledger must not be silently re-run under a v1 declaration."""
    from copy import deepcopy

    entries = deepcopy(registry.CAPABILITY_REGISTRY)
    target = next(entry for entry in entries if entry["capability_id"] == "operations.space.read")
    target["version"] = 1
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", entries)

    view = registry.effective_capability(db, "operations.space.read")
    assert view["unavailable_reason"] == "definition_out_of_sync"
    assert view["plan_executable"] is False
    with pytest.raises(registry.CapabilityError, match="definition_out_of_sync"):
        registry.execute_read(db, "operations.space.read", 1, actor=ADMIN, instance_id=12)
    result = registry.sync_declarations_at_startup(db)
    assert {"capability_id": "operations.space.read",
            "reason": "definition_version_regression"} in result["skipped"]


@pytest.mark.parametrize("capability_id", CAPABILITIES)
def test_unsupported_engine_is_refused_before_any_reader(db, capability_id):
    engine = UNSUPPORTED_ENGINE[capability_id]
    assert engine not in BY_ID[capability_id]["engines"]
    with pytest.raises(registry.CapabilityError, match="engine_not_supported"):
        registry.execute_read(db, capability_id, BY_ID[capability_id]["version"], actor=ADMIN,
                              instance_id=INSTANCE_IDS[engine])


@pytest.mark.parametrize("capability_id", CAPABILITIES)
def test_restricted_user_cannot_reach_these_handlers(db, capability_id):
    handler = BY_ID[capability_id]["handler"]
    # None of the six reads enumerate instances, so none may claim scope filtering.
    assert handler not in registry.INSTANCE_FILTERED_HANDLERS
    # Instance 11 is outside the user's scope.
    with pytest.raises(registry.CapabilityError, match="scope_denied"):
        registry.execute_read(db, capability_id, BY_ID[capability_id]["version"],
                              actor=RESTRICTED, instance_id=11)
    # Instance 10 is inside the scope, and the read is still refused because a handler that
    # ignores ctx.allowed_instance_ids must not be treated as scope-filtered.
    with pytest.raises(registry.CapabilityError, match="scope_denied"):
        registry.execute_read(db, capability_id, BY_ID[capability_id]["version"],
                              actor=RESTRICTED, instance_id=10)


INVALID_PARAMS = [
    ("operations.session.read", {"page_size": 201}),
    ("operations.session.read", {"page": 0}),
    ("operations.session.read", {"page": True}),
    ("operations.session.read", {"page_size": "50"}),
    ("performance.lock.read", {"page_size": 201}),
    ("performance.lock.read", {"page": -1}),
    ("performance.slow_sql.read", {"page_size": 201}),
    ("performance.slow_sql.read", {"time_range": "24h; DROP TABLE db_instance"}),
    ("performance.slow_sql.read", {"category": "top_avg' OR 1=1 --"}),
    ("performance.slow_sql.read", {"hours": 169}),
    ("operations.runtime_overview.read", {"bogus": "x"}),
    ("operations.space.read", {"engine": "oracle"}),
    ("operations.log.read", {"section": "wal"}),
    ("operations.session.read", {"filter": "user = root"}),
    ("performance.lock.read", {"sql": "select 1"}),
    ("performance.slow_sql.read", {"page_size": 201, "extra": "x"}),
]


@pytest.mark.parametrize("capability_id,params", INVALID_PARAMS,
                         ids=[f"{cap}-{index}" for index, (cap, _) in enumerate(INVALID_PARAMS)])
def test_invalid_params_are_refused(db, capability_id, params):
    # `sql` is intercepted earlier than pydantic on purpose; both outcomes are refusals, never
    # an execution, so the assertion only pins that no service function runs.
    with pytest.raises(registry.CapabilityError) as excinfo:
        registry.execute_read(db, capability_id, BY_ID[capability_id]["version"],
                              actor=ADMIN, instance_id=10, params=params)
    assert excinfo.value.code in {"invalid_params", "plan_raw_payload_rejected"}


def test_free_text_filter_field_is_not_expressible(db):
    with pytest.raises(registry.CapabilityError, match="invalid_params"):
        registry.execute_read(db, "operations.session.read", 2, actor=ADMIN,
                              instance_id=10, params={"user": "root' OR 1=1"})
