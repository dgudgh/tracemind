"""R3/R4: real connector lifecycle and readonly services with a synthetic driver."""

import importlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.db_instance import DBInstance
from app.services import parameters, slow_sql_analysis

connector = importlib.import_module("app.collectors.oracle.oracle_connection")
BUDGET_MS = COLLECTION_QUERY_TIMEOUT_SECONDS * 1000


class DriverFailure(RuntimeError):
    pass


class QueryTimeout(DriverFailure):
    pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, binds=None):
        state = self.connection.state
        state.queries.append((sql, binds, self.connection.call_timeout))
        if len(state.queries) in state.fail_queries:
            raise state.query_error
        normalized = sql.upper()
        assert normalized.startswith("SELECT ")
        if "COUNT(*) AS CNT FROM V$PARAMETER" in normalized:
            columns, self.rows = ["CNT"], [(1,)]
        elif "FROM V$PARAMETER" in normalized:
            columns = ["NAME", "VALUE", "DESCRIPTION", "ISDEFAULT", "ISSYS_MODIFIABLE"]
            self.rows = [("sessions", "200", "Sessions", "FALSE", "IMMEDIATE")]
        elif "NLS_" in normalized:
            columns, self.rows = ["PARAMETER", "VALUE"], [("NLS_CHARACTERSET", "AL32UTF8")]
        elif "CURRENT_PROCESSES" in normalized:
            columns, self.rows = ["CURRENT_PROCESSES"], [(2,)]
        elif "CURRENT_SESSIONS" in normalized:
            columns, self.rows = ["CURRENT_SESSIONS"], [(3,)]
        elif "COUNT(*)" in normalized:
            columns, self.rows = ["CNT"], [(0,)]
        elif "FROM DUAL" in normalized:
            columns, self.rows = ["CONTAINER_NAME"], [("TEST",)]
        else:
            columns, self.rows = [], []
        self.description = [(column,) for column in columns]

    def fetchall(self):
        return self.rows


class FakeConnection:
    version = "19.0.0.0.0"

    def __init__(self, state):
        self.state = state
        self._call_timeout = 0
        self.assignments = []
        self.close_calls = 0

    @property
    def call_timeout(self):
        return self._call_timeout

    @call_timeout.setter
    def call_timeout(self, value):
        self.assignments.append(value)
        if self.state.init_error:
            raise self.state.init_error
        self._call_timeout = value

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.close_calls += 1
        if self.state.close_error:
            raise self.state.close_error


@pytest.fixture
def oracle(monkeypatch):
    instance = DBInstance(
        id=987, instance_name="synthetic-r3r4", db_type="oracle",
        host="oracle.invalid", port=1521, username="monitor",
        password_cipher="synthetic", service_name="TEST", account_type="business",
    )
    state = SimpleNamespace(
        instance=instance, connections=[], connect_calls=[], queries=[],
        fail_queries=set(), query_error=QueryTimeout("synthetic query timeout"),
        init_error=None, close_error=None, connect_error=None,
    )

    def connect(**kwargs):
        state.connect_calls.append(kwargs)
        if state.connect_error:
            raise state.connect_error
        conn = FakeConnection(state)
        state.connections.append(conn)
        return conn

    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(
        connect=connect, makedsn=lambda *args, **kwargs: "synthetic-dsn", SYSDBA=2,
    ))
    monkeypatch.setattr(connector, "decrypt_password", lambda _: "synthetic-password")
    monkeypatch.setattr(connector, "oracle_wallet_settings", lambda _: {})
    monkeypatch.setattr(connector, "_oracle_failure_cache", {})
    monkeypatch.setattr(connector, "_oracle_connection_locks", {})
    return state


def assert_closed(oracle, count=1):
    assert len(oracle.connections) == count
    assert [conn.close_calls for conn in oracle.connections] == [1] * count
    key = connector._oracle_connection_key(oracle.instance)
    assert not connector._connection_lock(key).locked()


@pytest.mark.parametrize("timeout, expected", [(None, 0), (10000, 10000), (0, 1), (-1, 1), ("25", 25)])
def test_normal_exit_normalizes_timeout_and_closes_once(oracle, timeout, expected):
    with connector.oracle_connection(oracle.instance, call_timeout_ms=timeout) as conn:
        assert conn.call_timeout == expected
        assert conn.assignments == ([] if timeout is None else [expected])
        assert conn.close_calls == 0
    assert_closed(oracle)


@pytest.mark.parametrize("timeout", ["invalid", object(), float("nan"), float("inf")])
def test_invalid_timeout_never_connects_or_changes_cache(oracle, timeout):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        with connector.oracle_connection(oracle.instance, call_timeout_ms=timeout):
            pytest.fail("invalid timeout entered body")
    assert oracle.connect_calls == []
    assert connector._oracle_failure_cache == {}
    assert all(not lock.locked() for lock in connector._oracle_connection_locks.values())


def test_initialization_failure_closes_without_entering_body_or_caching(oracle):
    oracle.init_error = DriverFailure("synthetic initialization failure")
    with pytest.raises(DriverFailure) as raised:
        with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
            pytest.fail("initialization failure entered body")
    assert raised.value is oracle.init_error
    assert_closed(oracle)
    assert connector._oracle_failure_cache == {}
    oracle.init_error = None
    with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
        pass
    assert_closed(oracle, 2)


@pytest.mark.parametrize("body_fails, close_fails", [(True, False), (False, True), (True, True)])
def test_body_and_close_exception_precedence_is_preserved(oracle, body_fails, close_fails):
    body_error = DriverFailure("synthetic body failure")
    oracle.close_error = DriverFailure("synthetic close failure") if close_fails else None
    with pytest.raises(DriverFailure) as raised:
        with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
            if body_fails:
                raise body_error
    assert raised.value is (oracle.close_error if close_fails else body_error)
    if body_fails and close_fails:
        assert raised.value.__context__ is body_error
    assert_closed(oracle)
    assert connector._oracle_failure_cache == {}


def test_initialization_and_close_failures_keep_original_as_context(oracle):
    oracle.init_error = DriverFailure("synthetic initialization failure")
    oracle.close_error = DriverFailure("synthetic close failure")
    with pytest.raises(DriverFailure) as raised:
        with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
            pytest.fail("initialization failure entered body")
    assert raised.value is oracle.close_error
    assert raised.value.__context__ is oracle.init_error
    assert_closed(oracle)


def test_acquisition_failure_cache_force_live_and_lock_release(oracle):
    oracle.connect_error = DriverFailure("synthetic connect failure")
    with pytest.raises(DriverFailure) as raised:
        with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
            pytest.fail("failed acquisition entered body")
    assert raised.value is oracle.connect_error
    assert oracle.connections == []
    key = connector._oracle_connection_key(oracle.instance)
    assert not connector._connection_lock(key).locked()
    assert key in connector._oracle_failure_cache
    with pytest.raises(RuntimeError, match="synthetic connect failure") as cached:
        with connector.oracle_connection(oracle.instance, call_timeout_ms=10000):
            pytest.fail("cached failure entered body")
    assert "recent" in str(cached.value).lower() or "最近" in str(cached.value)
    assert len(oracle.connect_calls) == 1
    oracle.connect_error = None
    with connector.oracle_connection(oracle.instance, force_live=True, call_timeout_ms=10000):
        pass
    assert len(oracle.connect_calls) == 2
    assert connector._oracle_failure_cache == {}
    assert_closed(oracle)


@pytest.mark.parametrize("username, account_type", [("monitor", "business"), ("SYS", "sysdba")])
def test_connection_budget_and_retry_policy(oracle, monkeypatch, username, account_type):
    oracle.instance.username = username
    oracle.instance.account_type = account_type

    def unexpected_cursor(_connection):
        pytest.fail("cursor should not be used by this policy check")

    monkeypatch.setattr(FakeConnection, "cursor", unexpected_cursor)
    with connector.oracle_connection(oracle.instance):
        pass
    kwargs = oracle.connect_calls[0]
    assert kwargs["tcp_connect_timeout"] == 2.0
    assert kwargs["retry_count"] == 0
    assert kwargs["retry_delay"] == 0
    if account_type == "sysdba":
        assert kwargs["mode"] == sys.modules["oracledb"].SYSDBA
    assert_closed(oracle)


def test_inflight_connection_is_bounded_without_second_driver_call(oracle, monkeypatch):
    started, release = threading.Event(), threading.Event()
    errors = []

    def blocked_connect(**kwargs):
        oracle.connect_calls.append(kwargs)
        started.set()
        assert release.wait(5), "test did not release the synthetic driver"
        raise DriverFailure("synthetic slow connection failure")

    def first_connection():
        try:
            with connector.oracle_connection(oracle.instance):
                pass
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(sys.modules["oracledb"], "connect", blocked_connect)
    worker = threading.Thread(target=first_connection)
    worker.start()
    try:
        assert started.wait(2)
        before = time.monotonic()
        with pytest.raises(RuntimeError, match="正在进行"):
            with connector.oracle_connection(oracle.instance):
                pytest.fail("second connection entered body")
        assert time.monotonic() - before < 1.0
        assert len(oracle.connect_calls) == 1
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], DriverFailure)
    key = connector._oracle_connection_key(oracle.instance)
    assert not connector._connection_lock(key).locked()


READS = [
    ("get_parameters", 1, 2), ("get_sga_parameters", 2, 2),
    ("get_pga_parameters", 2, 2), ("get_process_parameters", 2, 2),
    ("get_session_parameters", 2, 3), ("get_password_policy", 1, 1),
    ("get_audit_config", 1, 1), ("get_nls_parameters", 1, 2),
    ("get_dblinks", 1, 1), ("get_datafile_parameters", 1, 1),
    ("get_supplemental_log", 1, 1), ("check_ransomware", 1, 5),
]


@pytest.mark.parametrize("name, connections, queries", READS)
def test_all_readonly_functions_wire_budget_through_real_connector(oracle, name, connections, queries):
    result = getattr(parameters, name)(oracle.instance)
    assert isinstance(result, dict)
    assert_closed(oracle, connections)
    assert len(oracle.queries) == queries
    assert {budget for _, _, budget in oracle.queries} == {BUDGET_MS}
    assert all(conn.assignments == [BUDGET_MS] for conn in oracle.connections)


def test_parameter_pagination_filter_and_nls_shapes(oracle):
    result = parameters.get_parameters(oracle.instance, "SES", page=2, page_size=5)
    assert result == {"items": [{"name": "sessions", "value": "200", "description": "Sessions",
                                  "isdefault": "FALSE", "issys_modifiable": "IMMEDIATE"}],
                      "total": 1, "page": 2, "page_size": 5}
    assert [binds for _, binds, _ in oracle.queries] == [{"kw": "%ses%"}] * 2
    assert "OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY" in oracle.queries[1][0]
    nls = [{"parameter": "NLS_CHARACTERSET", "value": "AL32UTF8"}]
    assert parameters.get_nls_parameters(oracle.instance) == {
        "database_parameters": nls, "instance_parameters": nls,
    }
    assert_closed(oracle, 2)


@pytest.mark.parametrize("name, query", [("get_parameters", 1), ("get_parameters", 2), ("get_nls_parameters", 1)])
def test_required_parameter_and_nls_query_timeouts_propagate_and_close(oracle, name, query):
    oracle.fail_queries = {query}
    with pytest.raises(QueryTimeout) as raised:
        getattr(parameters, name)(oracle.instance)
    assert raised.value is oracle.query_error
    assert_closed(oracle)
    assert oracle.queries[-1][2] == BUDGET_MS
    assert connector._oracle_failure_cache == {}


def test_optional_nls_query_timeout_keeps_existing_partial_shape(oracle):
    oracle.fail_queries = {2}
    assert parameters.get_nls_parameters(oracle.instance) == {
        "database_parameters": [{"parameter": "NLS_CHARACTERSET", "value": "AL32UTF8"}],
        "instance_parameters": [],
    }
    assert_closed(oracle)
    assert [budget for _, _, budget in oracle.queries] == [BUDGET_MS, BUDGET_MS]


@pytest.mark.parametrize("name", ["get_parameters", "get_nls_parameters"])
def test_read_initialization_failure_closes_before_any_query(oracle, name):
    oracle.init_error = DriverFailure("synthetic initialization failure")
    with pytest.raises(DriverFailure) as raised:
        getattr(parameters, name)(oracle.instance)
    assert raised.value is oracle.init_error
    assert oracle.queries == []
    assert_closed(oracle)


@pytest.mark.parametrize("stage", ["query", "initialization"])
def test_parameter_api_timeout_retains_degraded_pagination_contract(oracle, monkeypatch, stage):
    from app.api.v1 import oracle as api

    monkeypatch.setattr(api, "_get_oracle_instance", lambda *args: oracle.instance)
    error = oracle.query_error
    if stage == "query":
        oracle.fail_queries = {1}
    else:
        oracle.init_error = error = DriverFailure("synthetic initialization failure")
    result = api.oracle_parameters(987, keyword=None, page=2, page_size=5, db=None)
    assert set(result) == {"items", "total", "page", "page_size", "status", "warnings"}
    assert (result["items"], result["total"], result["page"], result["page_size"], result["status"]) == (
        [], 0, 2, 5, "degraded",
    )
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].endswith(str(error))
    assert_closed(oracle)
    if stage == "query":
        assert oracle.queries[0][2] == BUDGET_MS
    else:
        assert oracle.queries == []


@pytest.mark.parametrize("query", [1, 2])
def test_nls_api_preserves_required_failure_and_optional_fallback(oracle, monkeypatch, query):
    from app.api.v1 import oracle as api

    monkeypatch.setattr(api, "_get_oracle_instance", lambda *args: oracle.instance)
    oracle.fail_queries = {query}
    if query == 1:
        with pytest.raises(QueryTimeout) as raised:
            api.oracle_nls_params(987, db=None)
        assert raised.value is oracle.query_error
    else:
        assert api.oracle_nls_params(987, db=None) == {
            "database_parameters": [{"parameter": "NLS_CHARACTERSET", "value": "AL32UTF8"}],
            "instance_parameters": [],
        }
    assert_closed(oracle)
    assert oracle.queries[-1][2] == BUDGET_MS


@pytest.mark.parametrize("name, args", [("get_slow_sql_list", ()), ("get_sql_detail", ("synthetic",))])
def test_existing_slow_sql_budget_is_not_overridden(oracle, name, args):
    result = getattr(slow_sql_analysis, name)(oracle.instance, *args)
    assert result["status"] in {"success", "not_found"}
    assert oracle.queries
    assert {budget for _, _, budget in oracle.queries} == {10000}
    assert_closed(oracle)


def test_alter_system_remains_plan_only_without_a_connection(oracle):
    result = parameters.generate_alter_system_sql(oracle.instance, "sessions", "200", "memory")
    assert result["status"] == "plan_only"
    assert result["sql"] == "ALTER SYSTEM SET sessions = 200 SCOPE = MEMORY;"
    assert oracle.connect_calls == []
