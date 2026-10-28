"""OSS / 企业版可插拔边界：未安装时必须明确不可用，不得伪造结果。"""
from types import SimpleNamespace

import pytest

from app.services import engine_execution, engine_runtime
from app.services.engine_runtime import (
    READ_OPERATIONS,
    EngineRuntimeUnavailable,
    NullEngineRuntime,
    available,
    install,
    reset,
    runtime,
)

NULL_METHODS = [
    ("freeze", (None, None, "operations.session.read"), {}),
    ("check", (None,), {}),
    ("invoke", (None, 1), {"timeout": 5}),
    ("retain_plan", (None, None), {}),
    ("release_completed", (None,), {}),
    ("save_binding", (None, None, None), {}),
    ("check_binding", (None, None), {}),
    ("save_result", (None, None, None, {}, ), {"fence_epoch": 1}),
]


@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()


def test_uninstalled_reports_unavailable_instead_of_faking_success():
    assert available() is False
    assert isinstance(runtime(), NullEngineRuntime)
    assert runtime().is_null is True


@pytest.mark.parametrize("name,args,kwargs", NULL_METHODS)
def test_every_null_method_raises_a_typed_error(name, args, kwargs):
    with pytest.raises(EngineRuntimeUnavailable) as excinfo:
        getattr(runtime(), name)(*args, **kwargs)
    # 必须带稳定错误码：上游据此区分"未启用"与"执行失败"
    assert excinfo.value.code == "engine_runtime_unavailable"


def test_install_swaps_in_the_implementation_and_uninstall_restores_null():
    impl = SimpleNamespace(freeze=lambda *a, **k: "frozen")
    install(impl)
    assert available() is True
    assert runtime() is impl
    install(None)
    assert available() is False
    assert isinstance(runtime(), NullEngineRuntime)


def test_invalid_implementation_is_rejected_rather_than_silently_ignored():
    with pytest.raises(ValueError, match="engine_runtime_invalid"):
        install(object())


def test_read_operations_is_single_sourced():
    """能力目录依赖这份声明；两处各写一份必然漂移。"""
    assert engine_execution.READ_OPERATIONS == READ_OPERATIONS
    # 拷贝而非共享引用：任一处就地修改不得影响另一处
    assert engine_execution.READ_OPERATIONS is not READ_OPERATIONS


def test_legacy_bridge_guard_still_yields_none_when_not_installed():
    """`bridge() is not None` 是业务侧既有守卫，未安装时必须仍为 None。"""
    assert engine_execution.bridge() is None
    # 装一个非 EngineExecutionBridge 的实现后，bridge() 仍应为 None，
    # 否则业务会拿一个类型不对的对象去 freeze/invoke。
    install(SimpleNamespace(freeze=lambda *a, **k: None))
    assert engine_execution.bridge() is None


def test_install_bridge_rejects_foreign_objects():
    with pytest.raises(engine_execution.EngineExecutionError, match="engine_bridge_invalid"):
        engine_execution.install_bridge(SimpleNamespace(freeze=lambda *a, **k: None))
    # 失败后状态不得被改动
    assert available() is False
