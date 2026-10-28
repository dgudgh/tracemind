"""存储采集器：SSH iostat 采 await / %util / 队列深度。

数据源：auth={ssh_*}，或回退绑定实例 SSH 配置。
config：await_warn(20) util_warn(90) devices(可选，逗号设备名过滤)
"""
from __future__ import annotations

from app.collectors.diag.base import CollectContext, Signal, ssh_run, status_of, LAYER_STORAGE

_CMD = "iostat -x 1 2 | awk 'NR>1'"


def collect(ctx: CollectContext) -> list[Signal]:
    out = ssh_run(ctx, _CMD, timeout=15)
    if not out:
        return [Signal(LAYER_STORAGE, "storage", "reachable", 0, status="warn", node_type="storage",
                       phenomenon=f"存储指标采集失败：{ctx.name}",
                       judgement="无法 SSH 采集 iostat。",
                       impact="存储层不可观测。",
                       suggestion="确认 SSH 凭据与 sysstat(iostat) 安装。")]

    # 解析最后一段（第二次采样）的设备行；表头含 'Device'，await/%util 列名定位
    blocks = out.split("Device")
    seg = blocks[-1] if blocks else out
    lines = [l for l in seg.splitlines() if l.strip()]
    # 重新取一次表头以定位列
    header_cols = None
    for l in out.splitlines():
        if l.strip().startswith("r/s") or "await" in l and "%util" in l:
            header_cols = l.split()
            break

    dev_filter = None
    if ctx.cfg("devices"):
        dev_filter = {d.strip() for d in str(ctx.cfg("devices")).split(",") if d.strip()}

    signals: list[Signal] = []
    worst = None
    for l in lines:
        cols = l.split()
        if len(cols) < 6 or not cols[0][0].isalpha():
            continue
        dev = cols[0]
        if dev_filter and dev not in dev_filter:
            continue
        # 经典 iostat -x：await 通常倒数第 3 区，%util 最后一列
        try:
            util = float(cols[-1])
            await_ms = None
            # 尝试用表头定位 await
            if header_cols:
                hc = ["Device"] + header_cols
                if "await" in hc:
                    idx = hc.index("await")
                    if idx < len(cols):
                        await_ms = float(cols[idx])
            if await_ms is None and len(cols) >= 10:
                await_ms = float(cols[9])
        except (ValueError, IndexError):
            continue
        cand = {"dev": dev, "util": util, "await": await_ms}
        if worst is None or (util or 0) > worst["util"]:
            worst = cand

    if worst:
        st_u = status_of(worst["util"], ctx.cfg("util_warn", 90), ctx.cfg("util_crit", 98))
        if st_u != "ok":
            signals.append(Signal(
                LAYER_STORAGE, "storage", "disk_util", worst["util"], threshold=ctx.cfg("util_warn", 90),
                status=st_u, node_type="storage",
                phenomenon=f"磁盘 {worst['dev']} 利用率 {worst['util']}%",
                judgement="磁盘接近饱和，IO 已成瓶颈。",
                impact="存储饱和直接拉高数据库 IO 等待。",
                suggestion="定位高 IO 进程/SQL；评估存储扩展或分散负载。",
            ))
        if worst.get("await") is not None:
            st_a = status_of(worst["await"], ctx.cfg("await_warn", 20), ctx.cfg("await_crit", 50))
            if st_a != "ok":
                signals.append(Signal(
                    LAYER_STORAGE, "storage", "disk_await", worst["await"], threshold=ctx.cfg("await_warn", 20),
                    status=st_a, node_type="storage",
                    phenomenon=f"磁盘 {worst['dev']} await {worst['await']}ms",
                    judgement="单次 IO 时延高，存储响应慢。",
                    impact="高 await 表现为 db file sequential read / log file sync 升高。",
                    suggestion="排查存储链路（多路径/RAID/SAN）与队列深度。",
                ))
    if not signals and worst is None:
        signals.append(Signal(
            LAYER_STORAGE, "storage", "parse_status", 0, status="warn", node_type="storage",
            phenomenon=f"iostat 可执行但未解析到磁盘指标：{ctx.name}",
            judgement="iostat 输出格式不符合预期，或系统未返回设备明细。",
            impact="存储层状态不可观测，无法判断数据库等待是否由磁盘瓶颈导致。",
            suggestion="确认 sysstat 版本与 iostat -x 输出，必要时配置 devices 过滤或检查系统权限。",
            detail={"raw": out[:800]},
        ))
    return signals
