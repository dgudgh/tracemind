"""OS 采集器：SSH 采 CPU / load / 内存 / swap / iowait（Linux）。

数据源：auth={ssh_host/ssh_username/ssh_password/ssh_port}，或回退到绑定实例 SSH 配置。
config：cpu_warn(80) load_per_core_warn(1.5) mem_warn(90) swap_warn(20) iowait_warn(20)
"""
from __future__ import annotations

import re

from app.collectors.diag.base import CollectContext, Signal, ssh_run, status_of, LAYER_OS, LAYER_STORAGE


# 单条命令拿全部指标，减少 SSH 往返
_CMD = (
    "echo '__CPU__'; grep -c ^processor /proc/cpuinfo; "
    "echo '__LOAD__'; cat /proc/loadavg; "
    "echo '__MEM__'; free -m | awk '/Mem:/{print $2, $3} /Swap:/{print $2, $3}'; "
    "echo '__VMSTAT__'; vmstat 1 2 | tail -1"
)


def collect(ctx: CollectContext) -> list[Signal]:
    out = ssh_run(ctx, _CMD, timeout=15)
    if not out:
        return [Signal(LAYER_OS, "os", "reachable", 0, status="warn", node_type="os",
                       phenomenon=f"OS 指标采集失败：{ctx.name}",
                       judgement="无法 SSH 采集主机指标。",
                       impact="操作系统层不可观测，难以判断资源瓶颈。",
                       suggestion="确认 SSH 凭据与主机连通。")]

    sections: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("__") and line.endswith("__"):
            cur = line.strip("_")
            sections[cur] = []
        elif cur:
            sections[cur].append(line)

    signals: list[Signal] = []
    ncpu = 1
    try:
        ncpu = max(1, int(sections.get("CPU", ["1"])[0]))
    except (ValueError, IndexError):
        pass

    # load
    try:
        load1 = float(sections.get("LOAD", ["0"])[0].split()[0])
        per_core = round(load1 / ncpu, 2)
        st = status_of(per_core, ctx.cfg("load_per_core_warn", 1.5), ctx.cfg("load_per_core_crit", 3.0))
        signals.append(Signal(
            LAYER_OS, "os", "load_per_core", per_core, threshold=ctx.cfg("load_per_core_warn", 1.5),
            status=st, node_type="os",
            phenomenon=f"主机 load/核 {per_core}（load1 {load1}, {ncpu} 核）",
            judgement="单核负载偏高，CPU 或 IO 排队。" if st != "ok" else "负载正常。",
            impact="高负载会放大数据库会话等待与响应时间。",
            suggestion="结合 iowait/CPU 使用率判断是计算瓶颈还是 IO 瓶颈。",
        ))
    except (ValueError, IndexError):
        pass

    # mem / swap
    try:
        mem_lines = sections.get("MEM", [])
        if len(mem_lines) >= 1:
            mt, mu = mem_lines[0].split()[:2]
            mem_pct = round(int(mu) / max(1, int(mt)) * 100, 1)
            st = status_of(mem_pct, ctx.cfg("mem_warn", 90), ctx.cfg("mem_crit", 97))
            if st != "ok":
                signals.append(Signal(
                    LAYER_OS, "os", "mem_used_pct", mem_pct, threshold=ctx.cfg("mem_warn", 90),
                    status=st, node_type="os",
                    phenomenon=f"主机内存使用率 {mem_pct}%",
                    judgement="内存紧张，可能触发 swap 或缓存回收。",
                    impact="内存压力会影响数据库 SGA/PGA 与文件缓存。",
                    suggestion="排查内存大户与 HugePages/SGA 配置。",
                ))
        if len(mem_lines) >= 2:
            st_, su = mem_lines[1].split()[:2]
            if int(st_) > 0:
                swap_pct = round(int(su) / int(st_) * 100, 1)
                sst = status_of(swap_pct, ctx.cfg("swap_warn", 20), ctx.cfg("swap_crit", 50))
                if sst != "ok":
                    signals.append(Signal(
                        LAYER_OS, "os", "swap_used_pct", swap_pct, threshold=ctx.cfg("swap_warn", 20),
                        status=sst, node_type="os",
                        phenomenon=f"主机 Swap 使用率 {swap_pct}%",
                        judgement="发生 swap，内存严重不足。",
                        impact="swap 会显著拖慢数据库。",
                        suggestion="尽快释放/扩容内存，检查 SGA 与 HugePages。",
                    ))
    except (ValueError, IndexError):
        pass

    # iowait from vmstat last line (wa is column 16 in classic vmstat)
    try:
        vm = sections.get("VMSTAT", [])
        if vm:
            cols = vm[-1].split()
            # vmstat columns: ... us sy id wa st  → wa 通常倒数第 2/3
            wa = None
            if len(cols) >= 16:
                wa = float(cols[15])
            elif len(cols) >= 4:
                wa = float(cols[-2])
            if wa is not None:
                st = status_of(wa, ctx.cfg("iowait_warn", 20), ctx.cfg("iowait_crit", 40))
                if st != "ok":
                    signals.append(Signal(
                        LAYER_STORAGE, "os", "iowait", wa, threshold=ctx.cfg("iowait_warn", 20),
                        status=st, node_type="os",
                        phenomenon=f"主机 iowait {wa}%",
                        judgement="CPU 大量时间在等待 IO，存储/磁盘可能是瓶颈。",
                        impact="高 iowait 直接表现为 db file read / log file sync 等待升高。",
                        suggestion="结合 iostat await 与存储队列深度定位慢盘。",
                    ))
    except (ValueError, IndexError):
        pass

    return signals
