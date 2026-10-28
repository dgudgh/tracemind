"""未包含在本仓库的能力的占位实现。

本仓库不含这些能力，但部分既有调用点仍在。占位模块的作用是让调用**明确失败**，
而不是抛 `ModuleNotFoundError`（那会让整个进程起不来），也不是静默返回空值
（那会把"没做"伪装成"做了但没数据"）。
"""

from __future__ import annotations

UNAVAILABLE_MESSAGE = "此功能暂不支持"


class EditionFeatureUnavailable(RuntimeError):
    """调用未包含在本仓库的能力时抛出。"""

    code = "edition_capability_unavailable"

    def __init__(self, feature: str = ""):
        self.feature = feature
        super().__init__(UNAVAILABLE_MESSAGE)


def make_stub_getattr(module_name: str):
    def __getattr__(name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def _raise(*args, **kwargs):
            raise EditionFeatureUnavailable(f"{module_name}.{name}")

        return _raise

    return __getattr__
