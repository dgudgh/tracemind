"""Tomcat 采集器：HTTP manager status（线程池忙碌率）。

数据源：endpoint 指向 http://host:port/manager/status?XML=true
auth：{username, password}（Tomcat manager 账号）
config：busy_warn(0.8) busy_crit(0.95)
"""
from __future__ import annotations

import re

from app.collectors.diag.base import CollectContext, Signal, http_get, status_of, LAYER_APP


def collect(ctx: CollectContext) -> list[Signal]:
    url = ctx.endpoint
    if not url:
        return [Signal(LAYER_APP, "tomcat", "config", 0, status="warn", node_type="tomcat",
                       phenomenon=f"Tomcat 数据源未配置 endpoint：{ctx.name}",
                       judgement="Tomcat 采集缺少 manager status URL，无法读取线程池指标。",
                       impact="应用容器层状态不可观测，端到端诊断无法判断请求是否卡在应用线程池。",
                       suggestion="配置 http://host:port/manager/status?XML=true，并配置 manager 只读账号。")]
    auth = ctx.auth or {}
    basic = (auth.get("username"), auth.get("password")) if auth.get("username") else None
    text = http_get(url, timeout=ctx.cfg("timeout", 6), auth=basic)
    if not text:
        return [Signal(LAYER_APP, "tomcat", "reachable", 0, status="warn", node_type="tomcat",
                       phenomenon=f"Tomcat 状态不可达：{ctx.name}",
                       judgement="应用容器探测失败，可能 Tomcat 异常或 manager 未授权。",
                       impact="应用层不可观测。",
                       suggestion="确认 manager 账号与 Tomcat 进程状态。")]

    signals: list[Signal] = []
    # <threadInfo maxThreads="200" currentThreadCount="50" currentThreadsBusy="40"/>
    m_max = re.search(r'maxThreads="(\d+)"', text)
    m_busy = re.search(r'currentThreadsBusy="(\d+)"', text)
    m_cur = re.search(r'currentThreadCount="(\d+)"', text)
    if m_max and m_busy:
        max_t = int(m_max.group(1))
        busy = int(m_busy.group(1))
        ratio = round(busy / max_t, 3) if max_t else 0
        st = status_of(ratio, ctx.cfg("busy_warn", 0.8), ctx.cfg("busy_crit", 0.95))
        signals.append(Signal(
            LAYER_APP, "tomcat", "thread_busy_ratio", ratio,
            threshold=ctx.cfg("busy_warn", 0.8), status=st, node_type="tomcat",
            phenomenon=f"Tomcat 线程池忙碌率 {int(ratio*100)}%（{busy}/{max_t}）",
            judgement="线程池接近耗尽，常因下游（数据库/接口）响应慢导致线程被占满。" if st != "ok" else "线程池正常。",
            impact="线程池耗尽会导致请求排队、接口超时。",
            suggestion="排查慢接口与数据库慢 SQL/锁等待；评估线程池与连接池配置。",
            detail={"current": int(m_cur.group(1)) if m_cur else None},
        ))
    if not signals:
        signals.append(Signal(
            LAYER_APP, "tomcat", "parse_status", 0, status="warn", node_type="tomcat",
            phenomenon=f"Tomcat 状态页可达但未解析到线程池指标：{ctx.name}",
            judgement="返回内容不是 manager XML/status 格式，或账号权限不足导致没有 threadInfo。",
            impact="应用线程池状态不可观测，端到端诊断会缺少应用容器侧证据。",
            suggestion="确认 endpoint 使用 /manager/status?XML=true，并授予 manager-status 权限。",
            detail={"raw": text[:500]},
        ))
    return signals
