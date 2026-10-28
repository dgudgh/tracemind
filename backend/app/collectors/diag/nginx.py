"""Nginx 采集器：HTTP stub_status（活跃连接 / 等待队列）。

数据源：endpoint 指向 stub_status，如 http://host:port/nginx_status
config：active_warn(默认1000) active_crit(2000) waiting_warn(500)
"""
from __future__ import annotations

import re

from app.collectors.diag.base import CollectContext, Signal, http_get, status_of, LAYER_APP


def collect(ctx: CollectContext) -> list[Signal]:
    url = ctx.endpoint
    if not url:
        return [Signal(LAYER_APP, "nginx", "config", 0, status="warn", node_type="nginx",
                       phenomenon=f"Nginx 数据源未配置 endpoint：{ctx.name}",
                       judgement="Nginx 采集缺少 stub_status URL，无法读取入口层连接指标。",
                       impact="入口层状态不可观测，端到端诊断无法判断请求是否在网关侧堆积。",
                       suggestion="配置 http://host:port/nginx_status，并确认 Nginx 开启 stub_status。")]
    text = http_get(url, timeout=ctx.cfg("timeout", 6))
    if not text:
        return [Signal(LAYER_APP, "nginx", "reachable", 0, status="warn", node_type="nginx",
                       phenomenon=f"Nginx 状态页不可达：{ctx.name}",
                       judgement="反向代理层探测失败，可能 Nginx 异常或 stub_status 未开启。",
                       impact="入口层不可观测，无法确认上游转发是否正常。",
                       suggestion="确认 stub_status 配置与 Nginx 进程状态。")]

    # Active connections: N
    active = None
    m = re.search(r"Active connections:\s*(\d+)", text)
    if m:
        active = int(m.group(1))
    # Reading: x Writing: y Waiting: z
    waiting = None
    m2 = re.search(r"Waiting:\s*(\d+)", text)
    if m2:
        waiting = int(m2.group(1))

    signals: list[Signal] = []
    if active is not None:
        st = status_of(active, ctx.cfg("active_warn", 1000), ctx.cfg("active_crit", 2000))
        signals.append(Signal(
            LAYER_APP, "nginx", "active_connections", active,
            threshold=ctx.cfg("active_warn", 1000), status=st, node_type="nginx",
            phenomenon=f"Nginx 活跃连接 {active}",
            judgement="活跃连接高常伴随上游（Tomcat/数据库）处理慢导致连接堆积。" if st != "ok" else "连接数正常。",
            impact="入口连接堆积会放大用户侧响应时间。",
            suggestion="结合上游 Tomcat 线程池与数据库等待事件定位瓶颈。",
            detail={"raw": text[:200]},
        ))
    if waiting is not None:
        st = status_of(waiting, ctx.cfg("waiting_warn", 500), ctx.cfg("waiting_crit", 1000))
        signals.append(Signal(
            LAYER_APP, "nginx", "waiting_connections", waiting,
            threshold=ctx.cfg("waiting_warn", 500), status=st, node_type="nginx",
            phenomenon=f"Nginx 等待连接 {waiting}",
            judgement="keep-alive 等待连接过多，可能后端响应慢。" if st != "ok" else "等待连接正常。",
            impact="等待连接占用资源，影响新请求接入。",
            suggestion="检查 upstream 超时与后端处理时延。",
        ))
    if not signals:
        signals.append(Signal(
            LAYER_APP, "nginx", "parse_status", 0, status="warn", node_type="nginx",
            phenomenon=f"Nginx 状态页可达但未解析到 stub_status 指标：{ctx.name}",
            judgement="返回内容不是标准 stub_status 格式，或 endpoint 指向了普通页面。",
            impact="入口层连接数不可观测，端到端诊断会缺少网关侧证据。",
            suggestion="确认 endpoint 指向 stub_status location，并包含 Active connections / Reading / Writing / Waiting。",
            detail={"raw": text[:500]},
        ))
    return signals
