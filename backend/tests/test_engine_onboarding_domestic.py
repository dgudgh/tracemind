"""达梦 / 金仓的引擎接入竖切验收。

范围（本轮已实现的闭环）：实例 Schema 允许 → 连接测试分发到本引擎连接器 → 连接器缺驱动时给可操作错误。
范围外（未实现，本文件不假装覆盖）：巡检/容量/备份/业务操作路由仍只有原生四引擎端点，
详见 docs/known-issues.md 与 operations_capabilities.ENGINE_ORDER。
"""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import engines
from app.core.engines import DB_TYPE_PATTERN, DB_TYPES, ENGINE_MATURITY, EXTENSION_ENGINES

# 必须起别名：直接 import test_dm_connection 会让 pytest 把它当成用例收集，
# 报 "fixture 'instance' not found"。
from app.collectors.dm.dm_connection import test_dm_connection as dm_probe_connection
from app.collectors.kingbasees.kingbasees_connection import (
    test_kingbasees_connection as kingbase_probe_connection,
)
from app.schemas.db_instance import DBInstanceCreate
from app.services import instances as instances_service

import re


def _payload(db_type, **overrides):
    base = dict(
        instance_name="acc-" + db_type,
        db_type=db_type,
        host="127.0.0.1",
        port=5236 if db_type == "dm" else 54321,
        username="tester",
        password="Secret#123",
    )
    if db_type == "oracle":
        base.update(oracle_connect_type="service_name", service_name="ORCLPDB1")
    base.update(overrides)
    return base


@pytest.mark.parametrize("db_type", DB_TYPES)
def test_schema_accepts_every_declared_engine(db_type):
    model = DBInstanceCreate(**_payload(db_type))
    assert model.db_type == db_type


@pytest.mark.parametrize("bad", ["db2", "oscar", "informix", "DM", "", "oracle "])
def test_schema_rejects_engines_outside_the_allowlist(bad):
    """白名单必须精确：大小写和尾空格都不能被静默接受。"""
    with pytest.raises(ValidationError):
        DBInstanceCreate(**_payload(bad))


def test_pattern_is_derived_from_the_single_source():
    """正则与 DB_TYPES 必须同源，防止两处各写一份后漂移。"""
    compiled = re.compile(DB_TYPE_PATTERN)
    assert all(compiled.match(t) for t in DB_TYPES)
    assert not compiled.match("db2")
    assert set(DB_TYPES) == set(engines.NATIVE_ENGINES) | set(engines.EXTENSION_ENGINES)
    assert not (set(engines.NATIVE_ENGINES) & set(engines.EXTENSION_ENGINES))


def test_extension_engines_are_not_claimed_ready():
    """EP-R03：达梦/金仓没有真机验收证据，成熟度不得写成 ready。"""
    for engine in EXTENSION_ENGINES:
        assert ENGINE_MATURITY[engine] == "requires_runtime_validation"
        assert ENGINE_MATURITY[engine] != "ready"


@pytest.mark.parametrize("db_type", DB_TYPES)
def test_every_declared_engine_routes_to_its_own_connector(db_type, monkeypatch):
    """反向枚举护栏：DB_TYPES 里每个引擎都必须有独立分发分支。

    做法是把六个连接器全部替换成打标记的替身，然后逐个引擎调用；
    若某个引擎漏了分支，会落到 else 抛 Unsupported db_type，测试立刻变红。
    这就是「实例能建出来但连不上」那类半吊子接线的守门用例。
    """
    calls = []
    for name in ("test_oracle_connection", "test_mysql_connection", "test_postgresql_connection",
                 "test_sqlserver_connection", "test_dm_connection", "test_kingbasees_connection"):
        monkeypatch.setattr(instances_service, name,
                            lambda inst, _n=name, **_kw: calls.append(_n) or {"via": _n})
    instance = SimpleNamespace(
        id=1, db_type=db_type, instance_name="acc", host="127.0.0.1", port=1,
        account_type="system",
    )
    result = instances_service.test_connection(instance)
    expected = "test_" + ("kingbasees" if db_type == "kingbasees" else db_type) + "_connection"
    assert result["via"] == expected
    assert calls == [expected]
    assert db_type in DB_TYPES


def test_unknown_engine_still_fails_loudly(monkeypatch):
    """白名单之外不能静默返回成功。"""
    instance = SimpleNamespace(id=1, db_type="db2", instance_name="x", host="h", port=1,
                               account_type="system")
    with pytest.raises(ValueError, match="Unsupported db_type"):
        instances_service.test_connection(instance)


def test_dm_connector_reports_actionable_error_when_driver_absent():
    """dmPython 未安装时必须说清楚缺什么、且不能改用协议兼容驱动。"""
    instance = SimpleNamespace(host="127.0.0.1", port=5236, username="SYSDBA",
                               password_cipher=None, account_type="system")
    with pytest.raises(RuntimeError) as excinfo:
        dm_probe_connection(instance)
    message = str(excinfo.value)
    assert "dmPython" in message
    assert "不能改用" in message


def test_kingbasees_connector_reports_actionable_error_when_driver_absent():
    """ksycopg2 缺失时不能退回 psycopg2，否则无法区分金仓与 PostgreSQL。"""
    instance = SimpleNamespace(host="127.0.0.1", port=54321, username="system",
                               password_cipher=None, account_type="system")
    with pytest.raises(RuntimeError) as excinfo:
        kingbase_probe_connection(instance)
    message = str(excinfo.value)
    assert "ksycopg2" in message
    assert "不能" in message
