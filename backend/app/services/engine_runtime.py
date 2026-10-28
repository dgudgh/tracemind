"""引擎运行时协议与降级实现 —— OSS / 企业版的可插拔边界。

设计意图
--------
本模块**永久留在开源版**，且不包含任何引擎执行实现。它只做三件事：

1. 声明业务侧依赖的窄协议（`EngineRuntime`）；
2. 提供空实现（`NullEngineRuntime`），行为等价于"企业版未安装"；
3. 提供安装与读取入口（`install()` / `runtime()` / `available()`）。

企业版在启动时调用 `install()` 换入真实现。业务代码只依赖 `runtime()`，
从此不再直接 import 具体实现模块 —— 这是两个仓库能独立发布的前提。

为什么不直接用 entry_points
--------------------------
本项目不以 Python 包分发（无 pyproject/setup.py），而是源码直跑 + PyInstaller
单文件打包，`pip` 的 entry point 发现机制在这里不适用。因此采用"显式注册"：
企业版在自己的启动钩子里调用 `install()`；未调用即降级。

降级语义必须诚实
----------------
未安装企业版时，调用方拿到的不是"假成功"，而是明确的
`EngineRuntimeUnavailable`（或 `available() == False`）。宁可报未启用，
也不能让上游把"没有引擎运行时"当成"引擎返回了空结果"。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: 由引擎运行时承接的能力 ID。开源版必须保留这份声明：
#: 能力目录会据此判断某能力是否"由引擎侧执行"，缺了它能力目录会漂移。
READ_OPERATIONS: dict[str, str] = {"operations.session.read": "sessions"}

__all__ = [
    "READ_OPERATIONS",
    "EngineRuntime",
    "EngineRuntimeUnavailable",
    "NullEngineRuntime",
    "install",
    "runtime",
    "available",
    "reset",
]


class EngineRuntimeUnavailable(RuntimeError):
    """引擎运行时未安装。调用方必须显式处理，不能静默降级为成功。"""

    code = "engine_runtime_unavailable"

    def __init__(self, detail: str = "此功能暂不支持"):
        self.detail = detail
        super().__init__(self.code)


@runtime_checkable
class EngineRuntime(Protocol):
    """业务侧唯一依赖的接口面。

    方法签名与现有 `EngineExecutionBridge` 保持一致，以便企业版直接适配：
    换实现时业务代码零改动。新增方法必须先加到这里，再各自实现。
    """

    def freeze(self, db, instance, capability_id: str) -> Any: ...
    def check(self, frozen) -> Any: ...
    def invoke(self, frozen, instance_id: int, *, timeout: float) -> Any: ...
    def retain_plan(self, plan, frozen) -> Any: ...
    def release_completed(self, plan) -> Any: ...
    def save_binding(self, db, plan, frozen) -> Any: ...
    def check_binding(self, db, plan) -> Any: ...
    def save_result(self, db, plan, step, values, *, fence_epoch: int) -> Any: ...


class NullEngineRuntime:
    """空实现：所有能力一律报告"未启用"，绝不返回伪造结果。"""

    is_null = True

    def _raise(self, operation: str):
        raise EngineRuntimeUnavailable(f"此功能暂不支持：{operation}")

    def freeze(self, db, instance, capability_id):  # noqa: D102
        self._raise("freeze")

    def check(self, frozen):  # noqa: D102
        self._raise("check")

    def invoke(self, frozen, instance_id, *, timeout):  # noqa: D102
        self._raise("invoke")

    def retain_plan(self, plan, frozen):  # noqa: D102
        self._raise("retain_plan")

    def release_completed(self, plan):  # noqa: D102
        self._raise("release_completed")

    def save_binding(self, db, plan, frozen):  # noqa: D102
        self._raise("save_binding")

    def check_binding(self, db, plan):  # noqa: D102
        self._raise("check_binding")

    def save_result(self, db, plan, step, values, *, fence_epoch):  # noqa: D102
        self._raise("save_result")


_NULL = NullEngineRuntime()
_current: EngineRuntime | None = None


def install(implementation: EngineRuntime | None) -> None:
    """由企业版启动钩子调用。传 None 等价于卸载（便于测试与降级演练）。"""
    global _current
    if implementation is not None and not callable(getattr(implementation, "freeze", None)):
        raise ValueError("engine_runtime_invalid")
    _current = implementation


def runtime() -> EngineRuntime:
    """永远返回一个对象：未安装时返回空实现，调用方无需判空即可继续。"""
    return _current if _current is not None else _NULL


def available() -> bool:
    """供能力目录 / 页面 / 日志判断"企业版是否已启用"。"""
    return _current is not None


def reset() -> None:
    """仅测试使用：把状态清回未安装。"""
    install(None)
