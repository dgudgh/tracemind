"""把共享一致性检查 conformance.check 接到全部官方 provider 上。

背景：`app.engine_providers.conformance.check` 此前在全仓**零调用点**
（只有测试 import 了它内部的 `_Connection`），等于「读的顺序、行数上限、
字节上限、结果可序列化」这几条共享保证对任何 provider 都没被实际执行过。
本文件让它对 PROFILES 里声明的每个引擎真跑一遍。

2026-10-27 实测发现并修掉的真实缺陷：
`kingbasees.Provider` 没有 identity_sql / identity_fields，而 `base.invoke`
在返回 identify 结果**之前**就因 `identity_sql is None` 抛 identity_unavailable，
所以金仓当时 **5 个能力全部不可用，连 identify 都用不了**。
本文件的两条用例都会在当时的状态下变红，属于有效护栏而非恒真断言。
"""
from importlib import import_module

import pytest

from app.engine_providers.conformance import _Connection, check
from app.runtime.engine_sdk.profiles import PROFILES

# 每个引擎给一个「应当被接受」的真实 banner 和一个「必须被拒绝」的 banner。
# 拒绝侧的 banner 特意选成同族/近亲产品，防止靠 identity_tokens 太宽而蒙过去。
BANNERS = {
    'mysql': ('MySQL 8.0.36', 'MariaDB 10.6'),
    'oracle': ('Oracle Database 19c Enterprise Edition', 'Oracle Enterprise Linux 8'),
    'postgresql': ('PostgreSQL 15.4 on x86_64-pc-linux-gnu', 'KingbaseES V008R006'),
    'sqlserver': ('Microsoft SQL Server 2019 (RTM)', 'Azure SQL Database'),
    'dm': ('DM Database Server 64 V8', 'Oracle Database 19c'),
    'kingbasees': ('KingbaseES V008R006B0022', 'PostgreSQL 15.4 on x86_64-pc-linux-gnu'),
}


def _provider_of(engine_id):
    return import_module('app.engine_providers.' + PROFILES[engine_id]['module']).Provider


@pytest.mark.parametrize('engine_id', tuple(PROFILES))
def test_each_official_provider_satisfies_the_shared_conformance_contract(engine_id):
    banner, wrong_banner = BANNERS[engine_id]
    result = check(_provider_of(engine_id), banner, wrong_banner)
    assert result['engine_id'] == engine_id
    assert result['result'] == 'source_fixture_passed'
    # 源码一致性通过 ≠ 服务端已认证。这条必须一直是 False，直到有真机证据。
    assert result['server_certified'] is False


@pytest.mark.parametrize('engine_id', tuple(PROFILES))
def test_every_declared_operation_is_actually_invokable(engine_id):
    """注册了却调不通的能力等于没实现，必须逐项跑，不能只测 sessions。"""
    provider_class = _provider_of(engine_id)
    banner, _ = BANNERS[engine_id]
    fields = {key: 'fixture-' + key for key in provider_class.identity_fields}
    assert fields, '身份字段为空时其余能力必然全部 fail-closed'
    for operation in provider_class.queries:
        conn = _Connection(banner, fields)
        result = provider_class(lambda **_kwargs: conn).invoke(
            operation, {}, expected_identity=fields)
        assert result['capability_id'] == operation
        assert result['schema'] == 'engine.rows/v1'
        assert conn.closed, f'{engine_id}/{operation} 未关闭连接'


@pytest.mark.parametrize('engine_id', ('dm', 'kingbasees'))
def test_domestic_providers_never_reuse_a_native_driver(engine_id):
    """EP-R03：DM/金仓必须独立驱动，不能用协议兼容冒充 Oracle/PG。"""
    provider_class = _provider_of(engine_id)
    assert provider_class.driver not in PROFILES['oracle']['driver']
    assert provider_class.driver not in PROFILES['postgresql']['driver']
    assert provider_class.driver in ('dmPython', 'ksycopg2')
    identity_sql = (provider_class.identity_sql or '').lower()
    assert identity_sql, f'{engine_id} 缺 identity_sql 会导致全部能力不可用'
    # 达梦不得借用 Oracle 的 v$database，金仓不得借用 PG 的 pg_ 目录。
    assert 'pg_' not in identity_sql
    if engine_id == 'kingbasees':
        assert 'sys_control_system' in identity_sql
    else:
        assert 'v$' in identity_sql
