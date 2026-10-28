import ast
import importlib
from pathlib import Path
import pytest
from app.engine_providers.base import ProviderError
from app.engine_providers.conformance import _Connection, check

CASES = [('oracle', 'Oracle Database 19c'), ('mysql', '8.0 MySQL Community Server'),
         ('postgresql', 'PostgreSQL 16'), ('sqlserver', 'Microsoft SQL Server 2022')]

def setup(engine, banner):
    cls = importlib.import_module('app.engine_providers.' + engine).Provider
    fields = {key: 'fixture-' + key for key in cls.identity_fields}
    conn = _Connection(banner, fields)
    return cls(lambda **kw: conn), fields, conn

@pytest.mark.parametrize('engine,banner', CASES)
def test_fixed_queries_and_expected_asset(engine, banner):
    p, fields, conn = setup(engine, banner)
    result = p.invoke('sessions', {'host': 'fixture'}, expected_identity=fields)
    assert conn.current.executed == [p.queries['identify'], p.identity_sql, p.queries['sessions']]
    assert result['rows'] == [{'session_count': 1}] and result['instance_identity'] == fields and conn.closed
    with pytest.raises(ProviderError, match='expected_identity_required'): p.invoke('sessions', {})
    with pytest.raises(ProviderError, match='unsupported'): p.invoke('SELECT 1', {})
    with pytest.raises(ProviderError, match='invalid_parameters'): p.invoke('sessions', {}, {'sql': 'SELECT 1'})

@pytest.mark.parametrize('engine,banner', CASES)
def test_asset_mismatch_fails_before_data_query(engine, banner):
    p, fields, conn = setup(engine, banner)
    fields[next(iter(fields))] = 'different'
    with pytest.raises(ProviderError, match='identity_mismatch'): p.invoke('sessions', {}, expected_identity=fields)
    assert len(conn.current.executed) == 2 and conn.closed

@pytest.mark.parametrize('engine,banner', [('postgresql', 'Kingbase PostgreSQL'), ('mysql', 'MariaDB MySQL')])
def test_no_compatible_product_impersonation(engine, banner):
    p, fields, conn = setup(engine, banner)
    with pytest.raises(ProviderError, match='identity_mismatch'): p.invoke('sessions', {}, expected_identity=fields)
    assert len(conn.current.executed) == 1 and conn.closed

def test_identity_permission_failure_closed_and_redacted():
    p, fields, conn = setup('postgresql', 'PostgreSQL 16')
    original = conn.current.execute
    def execute(sql):
        if sql == p.identity_sql: raise RuntimeError('password=secret')
        return original(sql)
    conn.current.execute = execute
    with pytest.raises(ProviderError, match='identity_unavailable') as err: p.invoke('sessions', {}, expected_identity=fields)
    assert 'secret' not in str(err.value) and conn.closed

@pytest.mark.parametrize('engine,banner', CASES + [('dm', 'DM Database Server 64'), ('kingbasees', 'KingbaseES V8')])
def test_source_conformance(engine, banner):
    cls = importlib.import_module('app.engine_providers.' + engine).Provider
    assert check(cls, banner, 'not this product')['server_certified'] is False

def test_postgres_version_query_matches_legacy_source():
    source = Path(__file__).parents[1] / 'app/collectors/postgresql/postgresql_connection.py'
    strings = {n.value for n in ast.walk(ast.parse(source.read_text())) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert importlib.import_module('app.engine_providers.postgresql').Provider.queries['version'] in strings

@pytest.mark.parametrize('engine,banner', CASES)
def test_missing_asset_field_fails_closed(engine, banner):
    p, fields, conn = setup(engine, banner)
    conn.current.fields[next(iter(fields))] = None
    with pytest.raises(ProviderError, match='identity_unavailable'):
        p.invoke('sessions', {}, expected_identity=fields)
    assert len(conn.current.executed) == 2 and conn.closed


def test_postgres_old_new_version_and_asset_same_fixture(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.collectors.postgresql import postgresql_connection as legacy
    values = {'version': 'PostgreSQL 16', 'database_name': 'fixture_db', 'current_user': 'fixture_user'}
    class LegacyCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): self.sql = sql
        def fetchone(self):
            if 'current_database()' in self.sql: return values
            if 'pg_is_in_recovery()' in self.sql: return {'in_recovery': False, 'host_ip': '127.0.0.1', 'port': 5432}
            if 'pg_control_system()' in self.sql: return {'system_identifier': 'fixture-system_identifier'}
            return {'cnt': 1}
        def fetchall(self): return []
    class LegacyConn:
        def cursor(self): return LegacyCursor()
        def rollback(self): pass
    @contextmanager
    def connection(_instance): yield LegacyConn()
    monkeypatch.setattr(legacy, 'postgresql_connection', connection)
    instance = SimpleNamespace(host='fixture', port=5432, system_name=None, database_name='fixture_db', username='fixture_user')
    old = legacy.test_postgresql_connection(instance)
    cls = importlib.import_module('app.engine_providers.postgresql').Provider
    fields = {'system_identifier': 'fixture-system_identifier'}
    class NewCursor:
        def __init__(self): self.executed = []
        def execute(self, sql):
            self.executed.append(sql)
            row = ({'version': values['version']} if len(self.executed) == 1 else
                   fields if len(self.executed) == 2 else values)
            self.description = [(k,) for k in row]
            self.row = row
        def fetchmany(self, size): return [self.row]
        def close(self): pass
    class NewConn:
        def cursor(self): return NewCursor()
        def close(self): pass
    new = cls(lambda **kw: NewConn()).invoke('version', {}, expected_identity=fields)
    assert new['rows'][0] == values
    assert old['version'] == new['rows'][0]['version']
    assert old['database'] == new['rows'][0]['database_name']
    assert old['current_user'] == new['rows'][0]['current_user']
    assert old['architecture']['system_identifier'] == new['instance_identity']['system_identifier']
