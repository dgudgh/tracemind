#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace

from _bootstrap import add_backend_to_path

os.environ.setdefault("DEBUG", "false")
add_backend_to_path()

from app.services.diagnosis import ai_orchestrator, constants as C, hardware_profiler


SAMPLE_RAW = """__OS__
PRETTY_NAME="Oracle Linux Server 8.9"
5.15.0-204.147.6.el8uek.x86_64
dbhost01
__CPU__
Architecture:        x86_64
CPU(s):              16
Thread(s) per core:  2
Core(s) per socket:  8
Socket(s):           1
Model name:          Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz
Hypervisor vendor:   KVM
__MEM__
Mem:          64256       58000        2200        1000        4056        3000
Swap:          8192        4096        4096
HugePages_Total:    0
Hugepagesize:       2048 kB
__FS__
Filesystem     Type  1B-blocks       Used Available Use% Mounted on
/dev/sda2      xfs  107374182400 98784247808 8589934592 92% /
/dev/sdb1      xfs  214748364800 128849018880 85899345920 60% /u01
__LSBLK__
NAME="sda" TYPE="disk" SIZE="107374182400" MOUNTPOINT="" FSTYPE="" MODEL="Virtual Disk" ROTA="1"
NAME="sda2" TYPE="part" SIZE="107266808832" MOUNTPOINT="/" FSTYPE="xfs" MODEL="" ROTA="1"
NAME="sdb" TYPE="disk" SIZE="214748364800" MOUNTPOINT="" FSTYPE="" MODEL="DATA LUN" ROTA="0"
__IOSTAT__
Device            r/s     w/s     rkB/s     wkB/s   rrqm/s  wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz svctm %util
sda              5.00   20.00    100.00    800.00     0.00    0.00   0.00   0.00   35.00   42.00   1.25    20.00    40.00  0.80 95.00
sdb              1.00    2.00     50.00    100.00     0.00    0.00   0.00   0.00    5.00    6.00   0.02    50.00    50.00  0.50 10.00
__NET__
IFACE=eth0 SPEED=1000 DUPLEX=full RX_ERRORS=12 TX_ERRORS=3 MTU=1500 STATE=up
IFACE=lo SPEED=unknown DUPLEX=unknown RX_ERRORS=0 TX_ERRORS=0 MTU=65536 STATE=unknown
__RAID__
Personalities : [raid1]
md0 : active raid1 sda1[0] sdb1[1]
unused devices: <none>
__VIRT__
kvm
"""


def main() -> int:
    arch_info = {
        "tags": ["ASM"],
        "details": {
            "asm_diskgroups": [
                {"name": "DATA", "state": "CONNECTED", "total_mb": 102400, "free_mb": 4096, "used_pct": 96.0}
            ]
        },
    }
    profile = hardware_profiler.parse_hardware_profile(SAMPLE_RAW, arch_info=arch_info)

    assert profile["status"] == "ok"
    assert profile["cpu"]["logical_cpus"] == 16
    assert "Xeon" in profile["cpu"]["model_name"]
    assert profile["memory"]["total_mb"] == 64256
    assert profile["memory"]["swap_used_pct"] == 50.0
    assert profile["filesystems"][0]["mount"] == "/"
    assert profile["filesystems"][0]["used_pct"] == 92.0
    assert profile["io"]["worst_device"] == "sda"
    assert profile["io"]["await_ms"] == 42.0
    assert profile["network"][0]["iface"] == "eth0"
    assert profile["network"][0]["rx_errors"] == 12
    assert profile["virtualization"]["type"] == "kvm"
    assert profile["storage"]["asm"]["detected"] is True
    assert profile["storage"]["asm"]["diskgroups"][0]["used_pct"] == 96.0

    nodes = hardware_profiler.evidence_from_profile(profile)
    signatures = {(node["layer"], node["metric_name"]) for node in nodes}
    assert (C.LAYER_STORAGE, "hardware.fs.used_pct") in signatures
    assert (C.LAYER_STORAGE, "hardware.io.await_ms") in signatures
    assert (C.LAYER_NETWORK, "hardware.network.errors") in signatures
    assert (C.LAYER_STORAGE, "hardware.asm.used_pct") in signatures

    instance = SimpleNamespace(host="192.0.2.20", instance_name="mesdb", username="oracle")
    prompt = ai_orchestrator.build_prompt(
        instance,
        {
            "arch_type": "单机",
            "arch_tags": ["ASM"],
            "priority_template": {"note": "网络 > 存储 > 内存 > CPU"},
            "details": {"hardware_profile": profile},
        },
        [],
        [{"rank": 1, "layer": C.LAYER_STORAGE, "cause": "存储风险", "total_score": 0.5, "confidence": 72}],
    )
    assert "服务器硬件画像" in prompt
    assert "16 逻辑核" in prompt
    assert "Intel(R) Xeon" in prompt
    assert "根文件系统 / 使用率 92.0%" in prompt
    assert "最慢磁盘 sda await 42.0ms" in prompt
    assert "ASM DATA 使用率 96.0%" in prompt

    print("hardware profile policy ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
