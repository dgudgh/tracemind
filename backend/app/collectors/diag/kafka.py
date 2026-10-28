"""Kafka 采集器：消费组 lag（kafka-exporter /metrics 或 Burrow HTTP）。

数据源：endpoint 指向 exporter(/metrics) 或 Burrow 根；
config：mode(exporter|burrow，默认 exporter) cluster group lag_warn(10000) lag_crit(100000)
"""
from __future__ import annotations

import re

from app.collectors.diag.base import CollectContext, Signal, http_get, http_post_json, status_of, LAYER_APP
import requests


def _warn(ctx: CollectContext, metric: str, message: str, detail: dict | None = None) -> list[Signal]:
    return [Signal(
        LAYER_APP,
        "kafka",
        metric,
        0,
        status="warn",
        node_type="kafka",
        phenomenon=message,
        judgement="Kafka 采集未返回有效消费积压指标，可能是 exporter/Burrow 地址、权限、模式或指标名称不匹配。",
        impact="消息积压状态不可观测，端到端诊断无法判断业务延迟是否来自 MQ 堆积。",
        suggestion="确认数据源 endpoint、mode、cluster、group 配置，并检查 kafka-exporter /metrics 或 Burrow API 是否可访问。",
        detail=detail or {},
    )]


def _from_exporter(ctx: CollectContext) -> list[Signal]:
    url = ctx.endpoint if ctx.endpoint.endswith("/metrics") else ctx.endpoint.rstrip("/") + "/metrics"
    text = http_get(url, timeout=ctx.cfg("timeout", 6))
    if not text:
        return _warn(ctx, "reachable", f"Kafka exporter 不可达：{url}", {"url": url})
    total_lag = 0
    found = False
    for line in text.splitlines():
        if line.startswith("kafka_consumergroup_lag") and "}" in line:
            m = re.search(r"\}\s+([0-9.eE+-]+)$", line.strip())
            if m:
                try:
                    total_lag += int(float(m.group(1)))
                    found = True
                except ValueError:
                    pass
    if not found:
        return _warn(ctx, "consumer_lag_total", f"Kafka exporter 未发现 consumer lag 指标：{url}", {"url": url})
    st = status_of(total_lag, ctx.cfg("lag_warn", 10000), ctx.cfg("lag_crit", 100000))
    return [Signal(
        LAYER_APP, "kafka", "consumer_lag_total", total_lag, threshold=ctx.cfg("lag_warn", 10000),
        status=st, node_type="kafka",
        phenomenon=f"Kafka 消费总 lag {total_lag}",
        judgement="消费堆积，下游消费速度跟不上生产。" if st != "ok" else "消费 lag 正常。",
        impact="消息堆积会延迟业务处理，可能间接加压数据库批处理。",
        suggestion="排查消费者吞吐、分区分配与下游处理（含数据库写入）瓶颈。",
    )]


def _from_burrow(ctx: CollectContext) -> list[Signal]:
    cluster = ctx.cfg("cluster")
    group = ctx.cfg("group")
    if not (cluster and group):
        return _warn(ctx, "burrow_config", "Burrow 模式缺少 cluster 或 group 配置")
    url = f"{ctx.endpoint.rstrip('/')}/v3/kafka/{cluster}/consumer/{group}/lag"
    data = http_post_json(url, {}, timeout=ctx.cfg("timeout", 6)) or None
    if not data:
        try:
            data = requests.get(url, timeout=6).json()
        except Exception:
            return _warn(ctx, "reachable", f"Burrow API 不可达：{url}", {"url": url})
    status = (data.get("status") or {}) if isinstance(data, dict) else {}
    total = status.get("totallag")
    if total is None:
        return _warn(ctx, "consumer_lag_total", f"Burrow 未返回 totallag：{url}", {"url": url, "status": status})
    st = status_of(total, ctx.cfg("lag_warn", 10000), ctx.cfg("lag_crit", 100000))
    return [Signal(
        LAYER_APP, "kafka", "consumer_lag_total", total, threshold=ctx.cfg("lag_warn", 10000),
        status=st, node_type="kafka",
        phenomenon=f"Kafka 消费组 {group} 总 lag {total}",
        judgement=f"Burrow 状态 {status.get('status')}。" + ("消费堆积。" if st != "ok" else ""),
        impact="消息堆积会延迟业务处理。",
        suggestion="排查消费者吞吐与下游瓶颈。",
    )]


def collect(ctx: CollectContext) -> list[Signal]:
    if not ctx.endpoint:
        return _warn(ctx, "config", f"Kafka 数据源未配置 endpoint：{ctx.name}")
    try:
        if ctx.cfg("mode", "exporter") == "burrow":
            return _from_burrow(ctx)
        return _from_exporter(ctx)
    except Exception as exc:
        return _warn(ctx, "collector_error", f"Kafka 采集异常：{exc}", {"error": str(exc)})
