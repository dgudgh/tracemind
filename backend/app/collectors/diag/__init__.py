"""端到端诊断中心 多源采集器（中间件 / APM / OS / 存储）。

每个采集器暴露 collect(ctx) -> list[Signal]，全部防御式：任何异常返回部分/空结果，不抛出。
未配置 diag_data_source 时整层不参与，默认无副作用。
"""
