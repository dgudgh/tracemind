"""引擎驱动状态接口：只报本机可导入性，不得暗示已安装或已认证。"""
import importlib

import pytest

from app.api.v1 import engine_catalog
from app.runtime.engine_sdk.profiles import PROFILES


def test_driver_status_covers_every_declared_engine():
    payload = engine_catalog.engine_driver_status()
    assert payload["schema"] == "engine.driver_status/v1"
    assert [row["engine_id"] for row in payload["drivers"]] == list(PROFILES)
    for row in payload["drivers"]:
        assert row["declared_driver"] == PROFILES[row["engine_id"]]["driver"]
        assert isinstance(row["installed"], bool)
        assert isinstance(row["available_via"], list)
        # installed 必须与 available_via 自洽，不能一个说有一个说无
        assert row["installed"] is bool(row["available_via"])
        if not row["installed"]:
            assert row["note"], "缺失驱动必须给出可操作说明"


def test_driver_status_is_explicitly_scoped_to_import_only():
    payload = engine_catalog.engine_driver_status()
    assert payload["scope"] == "host_import_only"
    # 边界必须写在返回体里，避免前端把"驱动可导入"渲染成"引擎已认证"
    assert "不代表" in payload["disclaimer"]
    assert "认证" in payload["disclaimer"]


def test_postgresql_accepts_both_supported_drivers():
    """postgresql_connection 先试 psycopg2、失败再试 psycopg(v3)，两者都算可用。"""
    row = next(r for r in engine_catalog.engine_driver_status()["drivers"]
               if r["engine_id"] == "postgresql")
    assert set(row["candidates"]) == {"psycopg2", "psycopg"}


@pytest.mark.parametrize("engine_id,driver", [("dm", "dmPython"), ("kingbasees", "ksycopg2")])
def test_domestic_engines_never_fall_back_to_a_protocol_compatible_driver(engine_id, driver):
    """达梦/金仓不允许用协议兼容驱动顶替，候选集必须只有自己的驱动。"""
    row = next(r for r in engine_catalog.engine_driver_status()["drivers"]
               if r["engine_id"] == engine_id)
    assert row["candidates"] == [driver]
    for foreign in ("oracledb", "psycopg2", "psycopg", "pymysql", "pymssql"):
        assert foreign not in row["candidates"]


def test_module_probe_does_not_import_the_driver(monkeypatch):
    """探测必须是 find_spec，不能在请求路径上真 import（有驱动侧副作用）。"""
    import builtins
    calls = []
    real_import = builtins.__import__

    def spy(name, *args, **kwargs):
        calls.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", spy)
    engine_catalog.engine_driver_status()
    for name in ("dmPython", "ksycopg2", "pymssql", "pymysql"):
        assert name not in calls, f"{name} 被真实 import 了"
