"""Redis 采集器：RESP 协议直连 INFO（无需 redis 库）。

数据源：endpoint=host:port，auth={password}（可选）
config：blocked_warn(1) frag_warn(1.5)
"""
from __future__ import annotations

from app.collectors.diag.base import CollectContext, Signal, tcp_send_recv, status_of, LAYER_APP


def _parse_host_port(endpoint: str) -> tuple[str, int]:
    ep = (endpoint or "").replace("redis://", "").strip("/")
    if ":" in ep:
        host, port = ep.rsplit(":", 1)
        return host, int(port)
    return ep, 6379


def _info(host: str, port: int, password: str | None) -> dict:
    payload = b""
    if password:
        payload += f"AUTH {password}\r\n".encode()
    payload += b"INFO\r\n"
    raw = tcp_send_recv(host, port, payload, timeout=5)
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    info = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(("#", "$", "+", "-", "*")):
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    return info


def collect(ctx: CollectContext) -> list[Signal]:
    if not ctx.endpoint:
        return [Signal(LAYER_APP, "redis", "config", 0, status="warn", node_type="redis",
                       phenomenon=f"Redis 数据源未配置 endpoint：{ctx.name}",
                       judgement="Redis 采集缺少 host:port，无法读取 INFO 指标。",
                       impact="缓存层状态不可观测，端到端诊断无法判断 Redis 是否分担或放大数据库压力。",
                       suggestion="在数据源中配置 redis://host:port 或 host:port，并按需配置密码。")]
    host, port = _parse_host_port(ctx.endpoint)
    info = _info(host, port, (ctx.auth or {}).get("password"))
    if not info:
        return [Signal(LAYER_APP, "redis", "reachable", 0, status="warn", node_type="redis",
                       phenomenon=f"Redis 不可达：{ctx.name}",
                       judgement="缓存层探测失败，可能 Redis 异常或网络不通。",
                       impact="缓存不可用会把压力直接打到数据库。",
                       suggestion="确认 Redis 进程、密码与网络连通。")]

    signals: list[Signal] = []
    blocked = int(info.get("blocked_clients", 0) or 0)
    st = status_of(blocked, ctx.cfg("blocked_warn", 1), ctx.cfg("blocked_crit", 10))
    signals.append(Signal(
        LAYER_APP, "redis", "blocked_clients", blocked,
        threshold=ctx.cfg("blocked_warn", 1), status=st, node_type="redis",
        phenomenon=f"Redis 阻塞客户端 {blocked}",
        judgement="存在阻塞客户端（BLPOP/慢命令），可能拖慢依赖 Redis 的业务链路。" if st != "ok" else "无阻塞客户端。",
        impact="Redis 阻塞会传导为应用慢、数据库压力上升。",
        suggestion="排查慢命令（SLOWLOG）与大 key；评估持久化阻塞。",
        detail={"connected_clients": info.get("connected_clients")},
    ))
    try:
        frag = float(info.get("mem_fragmentation_ratio", 1) or 1)
        stf = status_of(frag, ctx.cfg("frag_warn", 1.5), ctx.cfg("frag_crit", 2.5))
        if stf != "ok":
            signals.append(Signal(
                LAYER_APP, "redis", "mem_fragmentation_ratio", round(frag, 2),
                threshold=ctx.cfg("frag_warn", 1.5), status=stf, node_type="redis",
                phenomenon=f"Redis 内存碎片率 {frag:.2f}",
                judgement="内存碎片偏高，可能影响性能与内存利用。",
                impact="内存压力可能触发淘汰或 swap。",
                suggestion="评估 activedefrag 或重启低峰整理。",
            ))
    except (TypeError, ValueError):
        pass
    return signals
