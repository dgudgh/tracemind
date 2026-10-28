import pytest
from app.engine_providers import base
from app.engine_providers.base import ProviderError
from app.engine_providers.conformance import _Connection
from app.engine_providers.dm import Provider as DM
from app.engine_providers.kingbasees import Provider as Kingbase


@pytest.mark.parametrize('provider', [DM, Kingbase])
@pytest.mark.parametrize('bad_identity', [
    {},
    None,
    {'db_magic': '1'},
    {'db_magic': '', 'instance_name': 'x'},
    {'db_magic': '0', 'instance_name': 'x'},
    {'db_magic': 1, 'instance_name': 'x'},
])
def test_missing_or_invalid_expected_identity_is_rejected_before_connecting(provider, bad_identity):
    """缺 / 坏 expected_identity 必须在打开连接之前就拒绝，不能"先读再说"。

    2026-10-27 修订：本条原先断言 kingbasees 因「没有 asset 身份」抛
    identity_unavailable。金仓补上 identity_sql 后该前提不再成立，
    但要求没有放松而是更严：现在断言的是「根本不建立连接」，
    用一个一旦被调用就抛 AssertionError 的 factory 来证明。
    """
    def factory(**_kwargs):
        raise AssertionError('身份校验通过前不应建立连接')

    with pytest.raises(ProviderError, match='expected_identity_required'):
        provider(factory).invoke('sessions', {}, expected_identity=bad_identity)


@pytest.mark.parametrize('provider', [DM, Kingbase])
def test_driver_absent_and_unsupported_are_distinct(provider, monkeypatch):
    def absent(name):
        raise ImportError(name)
    monkeypatch.setattr(base, 'import_module', absent)
    with pytest.raises(ProviderError, match='driver_unavailable'):
        provider().invoke('identify', {}, expected_identity={k: 'fixture' for k in provider.identity_fields})
    with pytest.raises(ProviderError, match='unsupported'):
        provider().invoke('write', {})


def test_dm_expected_magic_and_instance_match():
    fields = {'db_magic': '1234', 'instance_name': 'fixture'}
    conn = _Connection('DM Database Server 64', fields)
    result = DM(lambda **kw: conn).invoke('sessions', {}, expected_identity=fields)
    assert result['instance_identity'] == fields and result['rows'] == [{'session_count': 1}]
    wrong = _Connection('DM Database Server 64', fields)
    with pytest.raises(ProviderError, match='identity_mismatch'):
        DM(lambda **kw: wrong).invoke('sessions', {}, expected_identity={'db_magic': '2345', 'instance_name': 'fixture'})
    assert len(wrong.current.executed) == 2 and wrong.closed


def test_kingbase_expected_system_identifier_match_and_mismatch():
    """金仓的身份是 sys_control_system().system_identifier，必须逐值比对。"""
    fields = {'system_identifier': '7400000000000000001'}
    conn = _Connection('KingbaseES V008R006B0022', fields)
    result = Kingbase(lambda **kw: conn).invoke('version', {}, expected_identity=fields)
    assert result['instance_identity'] == fields
    assert result['rows'] == [{'session_count': 1}]
    wrong = _Connection('KingbaseES V008R006B0022', fields)
    with pytest.raises(ProviderError, match='identity_mismatch'):
        Kingbase(lambda **kw: wrong).invoke(
            'version', {}, expected_identity={'system_identifier': '7400000000000000002'})
    assert len(wrong.current.executed) == 2 and wrong.closed


def test_kingbase_never_claims_postgresql_identity():
    """金仓不能靠 PG 协议兼容冒充 PostgreSQL，反之亦然。"""
    kb_fields = {'system_identifier': '7400000000000000001'}
    pg_banner = _Connection('PostgreSQL 15.4 on x86_64-pc-linux-gnu', kb_fields)
    with pytest.raises(ProviderError, match='identity_mismatch'):
        Kingbase(lambda **kw: pg_banner).invoke('sessions', {}, expected_identity=kb_fields)
    assert len(pg_banner.current.executed) == 1 and pg_banner.closed
