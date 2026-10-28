"""Offline source conformance only; does not establish driver or server support."""
from .base import ProviderError

class _Cursor:
    def __init__(self, banner, fields): self.banner, self.fields, self.executed = banner, dict(fields), []
    def execute(self, sql):
        self.executed.append(sql)
        names = ['version'] if len(self.executed) == 1 else (list(self.fields) if len(self.executed) == 2 else ['session_count'])
        self.description = [(name,) for name in names]
    def fetchmany(self, size):
        if size != 101: raise AssertionError('unbounded fetch')
        return [(self.banner,)] if len(self.executed) == 1 else ([tuple(self.fields.values())] if len(self.executed) == 2 else [(1,)])
    def close(self): pass

class _Connection:
    def __init__(self, banner, fields): self.current, self.closed = _Cursor(banner, fields), False
    def cursor(self): return self.current
    def close(self): self.closed = True

def check(provider_class, banner, wrong_banner):
    fields = {key: 'fixture-' + key for key in provider_class.identity_fields}
    conn = _Connection(banner, fields)
    provider = provider_class(lambda **_kwargs: conn)
    if not provider.identity_sql:
        try: provider.invoke('sessions', {}, expected_identity=fields)
        except ProviderError as error:
            if error.code != 'identity_unavailable': raise
        else: raise AssertionError('unverified_identity_accepted')
        return {'engine_id': provider.engine_id, 'result': 'identity_unavailable', 'server_certified': False}
    result = provider.invoke('sessions', {}, expected_identity=fields)
    if result['engine_id'] != provider.engine_id or result['rows'] != [{'session_count': 1}]: raise AssertionError('result_mismatch')
    if conn.current.executed != [provider.queries['identify'], provider.identity_sql, provider.queries['sessions']] or not conn.closed:
        raise AssertionError('read_or_close_mismatch')
    other = _Connection(wrong_banner, fields)
    try: provider_class(lambda **_kwargs: other).invoke('sessions', {}, expected_identity=fields)
    except ProviderError as error:
        if error.code != 'identity_mismatch': raise
    else: raise AssertionError('identity_not_rejected')
    if len(other.current.executed) != 1 or not other.closed: raise AssertionError('read_after_identity_failure')
    try: provider.invoke('arbitrary_sql', {}, expected_identity=fields)
    except ProviderError as error:
        if error.code != 'unsupported': raise
    else: raise AssertionError('unregistered_operation')
    return {'engine_id': provider.engine_id, 'result': 'source_fixture_passed', 'server_certified': False}
