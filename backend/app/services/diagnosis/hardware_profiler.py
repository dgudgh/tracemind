"""Server hardware and OS profile collection for diagnosis.

The profile is a base fact set for AI diagnosis. Abnormal hardware signals are
also converted into evidence nodes so RCA can rank storage/OS/network causes.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

from app.models.db_instance import DBInstance
from app.services.diagnosis import constants as C
from app.services.ssh_executor import execute_command


PROFILE_COMMAND = r"""
echo __OS__
(grep '^PRETTY_NAME=' /etc/os-release 2>/dev/null || true)
uname -r 2>/dev/null || true
hostname 2>/dev/null || true
echo __CPU__
(lscpu 2>/dev/null || cat /proc/cpuinfo 2>/dev/null | head -80 || true)
echo __MEM__
(free -m 2>/dev/null || true)
(grep -E '^(HugePages_Total|HugePages_Free|Hugepagesize):' /proc/meminfo 2>/dev/null || true)
echo __FS__
(df -PT -B1 -x tmpfs -x devtmpfs -x squashfs 2>/dev/null || df -hT 2>/dev/null || true)
echo __LSBLK__
(lsblk -b -P -o NAME,TYPE,SIZE,MOUNTPOINT,FSTYPE,MODEL,ROTA 2>/dev/null || true)
echo __IOSTAT__
(iostat -x 1 2 2>/dev/null || true)
echo __NET__
for dev in /sys/class/net/*; do
  iface=$(basename "$dev")
  [ "$iface" = "lo" ] && continue
  speed=$(cat "$dev/speed" 2>/dev/null || echo unknown)
  duplex=$(cat "$dev/duplex" 2>/dev/null || echo unknown)
  rx=$(cat "$dev/statistics/rx_errors" 2>/dev/null || echo 0)
  tx=$(cat "$dev/statistics/tx_errors" 2>/dev/null || echo 0)
  mtu=$(cat "$dev/mtu" 2>/dev/null || echo unknown)
  state=$(cat "$dev/operstate" 2>/dev/null || echo unknown)
  echo "IFACE=$iface SPEED=$speed DUPLEX=$duplex RX_ERRORS=$rx TX_ERRORS=$tx MTU=$mtu STATE=$state"
done
echo __RAID__
(cat /proc/mdstat 2>/dev/null || true)
(command -v multipath >/dev/null 2>&1 && multipath -ll 2>/dev/null | head -80 || true)
(lspci 2>/dev/null | grep -Ei 'raid|sas|scsi|fibre|ethernet' | head -40 || true)
echo __VIRT__
(systemd-detect-virt 2>/dev/null || true)
(command -v virt-what >/dev/null 2>&1 && virt-what 2>/dev/null || true)
""".strip()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _sections(raw: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("__") and stripped.endswith("__"):
            current = stripped.strip("_")
            sections[current] = []
            continue
        if current:
            sections[current].append(line.rstrip())
    return sections


def _parse_cpu(lines: list[str]) -> dict[str, Any]:
    text = "\n".join(lines)
    cpu: dict[str, Any] = {}
    for line in lines:
        if ":" not in line and "：" not in line:
            continue
        key, value = [part.strip() for part in re.split(r"[:：]", line, maxsplit=1)]
        low = key.lower()
        if low in ("model name", "型号名称"):
            cpu["model_name"] = value
        elif low in ("cpu(s)", "cpu"):
            cpu["logical_cpus"] = _to_int(value)
        elif low in ("thread(s) per core", "每个核的线程数"):
            cpu["threads_per_core"] = _to_int(value)
        elif low in ("core(s) per socket", "每个座的核数"):
            cpu["cores_per_socket"] = _to_int(value)
        elif low in ("socket(s)", "座"):
            cpu["sockets"] = _to_int(value)
        elif low in ("architecture", "架构"):
            cpu["architecture"] = value
        elif low in ("hypervisor vendor", "超管理器厂商"):
            cpu["hypervisor_vendor"] = value
    if "model_name" not in cpu:
        match = re.search(r"model name\s*:\s*(.+)", text)
        if match:
            cpu["model_name"] = match.group(1).strip()
    if "logical_cpus" not in cpu:
        cpu["logical_cpus"] = len(re.findall(r"^processor\s*:", text, flags=re.MULTILINE)) or None
    return {k: v for k, v in cpu.items() if v not in (None, "")}


def _parse_memory(lines: list[str]) -> dict[str, Any]:
    memory: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Mem:"):
            parts = stripped.split()
            if len(parts) >= 3:
                total = _to_int(parts[1])
                used = _to_int(parts[2])
                memory.update(
                    {
                        "total_mb": total,
                        "used_mb": used,
                        "used_pct": round(used / max(1, total) * 100, 1),
                    }
                )
                if len(parts) >= 7:
                    memory["available_mb"] = _to_int(parts[6])
        elif stripped.startswith("Swap:"):
            parts = stripped.split()
            if len(parts) >= 3:
                total = _to_int(parts[1])
                used = _to_int(parts[2])
                memory.update(
                    {
                        "swap_total_mb": total,
                        "swap_used_mb": used,
                        "swap_used_pct": round(used / max(1, total) * 100, 1) if total else 0.0,
                    }
                )
        elif stripped.startswith("HugePages_Total:"):
            memory["hugepages_total"] = _to_int(stripped.split(":", 1)[1])
        elif stripped.startswith("HugePages_Free:"):
            memory["hugepages_free"] = _to_int(stripped.split(":", 1)[1])
        elif stripped.startswith("Hugepagesize:"):
            memory["hugepage_size_kb"] = _to_int(stripped.split(":", 1)[1].split()[0])
    return memory


def _parse_filesystems(lines: list[str]) -> list[dict[str, Any]]:
    filesystems: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("Filesystem") or stripped.startswith("文件系统"):
            continue
        parts = stripped.split()
        if len(parts) < 7:
            continue
        fs, fs_type, size, used, available, used_pct, mount = parts[:7]
        if not str(used_pct).endswith("%"):
            continue
        filesystems.append(
            {
                "filesystem": fs,
                "type": fs_type,
                "size_bytes": _to_int(size),
                "used_bytes": _to_int(used),
                "available_bytes": _to_int(available),
                "used_pct": _to_float(used_pct),
                "mount": mount,
            }
        )
    return filesystems


def _parse_key_values(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in shlex.split(line):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        result[key] = value
    return result


def _parse_lsblk(lines: list[str]) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in lines:
        values = _parse_key_values(line.strip())
        if not values:
            continue
        disks.append(
            {
                "name": values.get("NAME"),
                "type": values.get("TYPE"),
                "size_bytes": _to_int(values.get("SIZE")),
                "mount": values.get("MOUNTPOINT") or "",
                "fstype": values.get("FSTYPE") or "",
                "model": values.get("MODEL") or "",
                "rotational": values.get("ROTA") == "1",
            }
        )
    return disks


def _parse_iostat(lines: list[str]) -> dict[str, Any]:
    headers: list[str] | None = None
    current_worst: dict[str, Any] | None = None
    final_worst: dict[str, Any] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Device"):
            if current_worst:
                final_worst = current_worst
            headers = [item.rstrip(":") for item in stripped.split()]
            current_worst = None
            continue
        parts = stripped.split()
        if not headers or len(parts) < 2 or parts[0].lower() == "linux":
            continue
        values = {headers[idx]: parts[idx] for idx in range(min(len(headers), len(parts)))}
        device = values.get("Device")
        if not device:
            continue
        util = _to_float(values.get("%util"))
        awaits = [_to_float(values.get("await")), _to_float(values.get("r_await")), _to_float(values.get("w_await"))]
        await_ms = max(awaits)
        candidate = {
            "worst_device": device,
            "await_ms": round(await_ms, 2),
            "util_pct": round(util, 2),
            "aqu_sz": _to_float(values.get("aqu-sz")),
        }
        if current_worst is None or candidate["await_ms"] > current_worst.get("await_ms", 0) or candidate["util_pct"] > current_worst.get("util_pct", 0):
            current_worst = candidate
    return current_worst or final_worst or {}


def _parse_network(lines: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in lines:
        values = _parse_key_values(line.strip())
        if not values or values.get("IFACE") == "lo":
            continue
        result.append(
            {
                "iface": values.get("IFACE"),
                "speed_mbps": _to_int(values.get("SPEED"), default=0),
                "duplex": values.get("DUPLEX") or "unknown",
                "rx_errors": _to_int(values.get("RX_ERRORS")),
                "tx_errors": _to_int(values.get("TX_ERRORS")),
                "mtu": _to_int(values.get("MTU")),
                "state": values.get("STATE") or "unknown",
            }
        )
    return result


def _parse_virtualization(lines: list[str], cpu: dict[str, Any]) -> dict[str, Any]:
    virt_type = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            virt_type = stripped
            break
    if not virt_type and cpu.get("hypervisor_vendor"):
        virt_type = str(cpu["hypervisor_vendor"]).lower()
    return {"type": virt_type or "unknown"}


def _parse_raid(lines: list[str]) -> dict[str, Any]:
    text = "\n".join(lines).strip()
    lowered = text.lower()
    degraded = bool(re.search(r"\[[u_]+\]", lowered) and "_" in lowered)
    return {
        "detected": bool(text and ("raid" in lowered or "multipath" in lowered or "md" in lowered)),
        "degraded": degraded,
        "summary": text[:1000],
    }


def _asm_from_arch(arch_info: dict | None) -> dict[str, Any]:
    arch_info = arch_info or {}
    tags = arch_info.get("tags") or arch_info.get("arch_tags") or []
    details = arch_info.get("details") or {}
    diskgroups = details.get("asm_diskgroups") or []
    return {"detected": "ASM" in tags or bool(diskgroups), "diskgroups": diskgroups}


def parse_hardware_profile(raw: str, arch_info: dict | None = None) -> dict[str, Any]:
    sections = _sections(raw)
    cpu = _parse_cpu(sections.get("CPU", []))
    profile = {
        "status": "ok",
        "os": {
            "pretty_name": next((line.split("=", 1)[1].strip('"') for line in sections.get("OS", []) if line.startswith("PRETTY_NAME=")), ""),
            "kernel": next((line.strip() for line in sections.get("OS", []) if line.strip() and not line.startswith("PRETTY_NAME=")), ""),
            "hostname": (sections.get("OS", ["", "", ""]) + ["", "", ""])[2].strip(),
        },
        "cpu": cpu,
        "memory": _parse_memory(sections.get("MEM", [])),
        "filesystems": _parse_filesystems(sections.get("FS", [])),
        "block_devices": _parse_lsblk(sections.get("LSBLK", [])),
        "io": _parse_iostat(sections.get("IOSTAT", [])),
        "network": _parse_network(sections.get("NET", [])),
        "virtualization": _parse_virtualization(sections.get("VIRT", []), cpu),
        "storage": {
            "raid": _parse_raid(sections.get("RAID", [])),
            "asm": _asm_from_arch(arch_info),
        },
    }
    return profile


def unavailable_profile(message: str) -> dict[str, Any]:
    return {"status": "unavailable", "message": message}


def collect_hardware_profile(instance: DBInstance, arch_info: dict | None = None) -> dict[str, Any]:
    if not getattr(instance, "ssh_host", None):
        return unavailable_profile("实例未配置 SSH，无法采集服务器硬件画像。")
    result = execute_command(instance, PROFILE_COMMAND, timeout=45)
    if not result.get("ok"):
        return unavailable_profile(result.get("stderr") or result.get("error") or "硬件画像采集失败。")
    profile = parse_hardware_profile(result.get("stdout") or "", arch_info=arch_info)
    profile["source"] = "ssh"
    return profile


def _severity(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "critical"
    if value >= warn:
        return "warning"
    return "info"


def evidence_from_profile(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not profile or profile.get("status") != "ok":
        return []
    nodes: list[dict[str, Any]] = []

    for fs in profile.get("filesystems") or []:
        used = _to_float(fs.get("used_pct"))
        if used >= 85:
            sev = _severity(used, 85, 95)
            nodes.append(
                {
                    "layer": C.LAYER_STORAGE,
                    "source": "hardware_profile",
                    "phenomenon": f"文件系统 {fs.get('mount')} 使用率 {used}%",
                    "evidence": f"{fs.get('filesystem')} {fs.get('type')} size={fs.get('size_bytes')} used={fs.get('used_bytes')}",
                    "judgement": "文件系统空间压力可能影响数据库写入、归档、审计或日志落盘。",
                    "impact": "空间不足会导致数据库对象扩展失败、归档失败或服务异常。",
                    "suggestion": "清理无用文件、确认归档/备份保留策略，必要时扩容文件系统。",
                    "metric_name": "hardware.fs.used_pct",
                    "metric_value": str(used),
                    "threshold_value": ">=85% warning / >=95% critical",
                    "weight": 0.9 if sev == "critical" else 0.7,
                    "confidence": 82,
                    "severity": sev,
                    "raw": fs,
                }
            )

    io = profile.get("io") or {}
    await_ms = _to_float(io.get("await_ms"))
    util = _to_float(io.get("util_pct"))
    if await_ms >= 20 or util >= 90:
        sev = "critical" if await_ms >= 50 or util >= 98 else "warning"
        nodes.append(
            {
                "layer": C.LAYER_STORAGE,
                "source": "hardware_profile",
                "phenomenon": f"磁盘 {io.get('worst_device')} await {await_ms}ms / util {util}%",
                "evidence": f"iostat: await={await_ms}ms, util={util}%, aqu-sz={io.get('aqu_sz')}",
                "judgement": "磁盘 IO 响应慢或利用率饱和，可能是数据库等待升高的上游原因。",
                "impact": "会放大 db file read、log file sync、归档和批处理耗时。",
                "suggestion": "检查高 IO SQL/进程、存储队列、RAID/多路径和底层存储性能。",
                "metric_name": "hardware.io.await_ms",
                "metric_value": str(await_ms),
                "threshold_value": "await>=20ms 或 util>=90%",
                "weight": 0.9 if sev == "critical" else 0.75,
                "confidence": 82,
                "severity": sev,
                "raw": io,
            }
        )

    mem = profile.get("memory") or {}
    mem_pct = _to_float(mem.get("used_pct"))
    swap_pct = _to_float(mem.get("swap_used_pct"))
    if mem_pct >= 90 or swap_pct >= 20:
        sev = "critical" if mem_pct >= 97 or swap_pct >= 50 else "warning"
        nodes.append(
            {
                "layer": C.LAYER_OS,
                "source": "hardware_profile",
                "phenomenon": f"主机内存使用率 {mem_pct}% / Swap {swap_pct}%",
                "evidence": f"total={mem.get('total_mb')}MB used={mem.get('used_mb')}MB swap_used={mem.get('swap_used_mb')}MB",
                "judgement": "内存或 Swap 压力可能影响数据库缓存、PGA/排序和系统响应。",
                "impact": "Swap 会显著拖慢数据库，内存不足会引发缓存抖动。",
                "suggestion": "检查数据库内存参数、HugePages、异常进程和连接数。",
                "metric_name": "hardware.memory.used_pct",
                "metric_value": str(max(mem_pct, swap_pct)),
                "threshold_value": "mem>=90% 或 swap>=20%",
                "weight": 0.85 if sev == "critical" else 0.65,
                "confidence": 78,
                "severity": sev,
                "raw": mem,
            }
        )

    for nic in profile.get("network") or []:
        errors = _to_int(nic.get("rx_errors")) + _to_int(nic.get("tx_errors"))
        speed = _to_int(nic.get("speed_mbps"))
        if errors > 0 or (0 < speed < 1000):
            nodes.append(
                {
                    "layer": C.LAYER_NETWORK,
                    "source": "hardware_profile",
                    "phenomenon": f"网卡 {nic.get('iface')} 错误包 {errors} / 速率 {speed or 'unknown'}Mbps",
                    "evidence": f"rx_errors={nic.get('rx_errors')} tx_errors={nic.get('tx_errors')} duplex={nic.get('duplex')} state={nic.get('state')}",
                    "judgement": "网卡错误或低速率可能导致数据库连接抖动、复制延迟或 RAC 互联异常。",
                    "impact": "网络问题会放大连接超时、Data Guard/复制延迟和应用响应抖动。",
                    "suggestion": "检查交换机端口、双工/速率协商、网卡驱动和错误包增长趋势。",
                    "metric_name": "hardware.network.errors",
                    "metric_value": str(errors),
                    "threshold_value": ">0 errors 或 speed<1000Mbps",
                    "weight": 0.75,
                    "confidence": 76,
                    "severity": "warning",
                    "raw": nic,
                }
            )

    asm = ((profile.get("storage") or {}).get("asm") or {})
    for dg in asm.get("diskgroups") or []:
        used = _to_float(dg.get("used_pct"))
        if used >= 85:
            sev = _severity(used, 85, 95)
            nodes.append(
                {
                    "layer": C.LAYER_STORAGE,
                    "source": "hardware_profile",
                    "phenomenon": f"ASM 磁盘组 {dg.get('name')} 使用率 {used}%",
                    "evidence": f"state={dg.get('state')} total_mb={dg.get('total_mb')} free_mb={dg.get('free_mb')}",
                    "judgement": "ASM 磁盘组容量压力会影响数据文件、归档和恢复区扩展。",
                    "impact": "ASM 空间不足可能导致数据库写入失败或归档中断。",
                    "suggestion": "检查 ASM 磁盘组、归档/FRA 和数据文件增长，按变更流程扩容。",
                    "metric_name": "hardware.asm.used_pct",
                    "metric_value": str(used),
                    "threshold_value": ">=85% warning / >=95% critical",
                    "weight": 0.9 if sev == "critical" else 0.7,
                    "confidence": 84,
                    "severity": sev,
                    "raw": dg,
                }
            )

    raid = ((profile.get("storage") or {}).get("raid") or {})
    if raid.get("degraded"):
        nodes.append(
            {
                "layer": C.LAYER_STORAGE,
                "source": "hardware_profile",
                "phenomenon": "RAID/多路径存在降级迹象",
                "evidence": raid.get("summary"),
                "judgement": "RAID 或多路径降级会降低冗余能力并可能放大 IO 延迟。",
                "impact": "存储链路异常会影响数据库读写延迟和可用性。",
                "suggestion": "检查 mdadm/multipath/存储阵列状态并恢复冗余。",
                "metric_name": "hardware.raid.degraded",
                "metric_value": "1",
                "threshold_value": "degraded=false",
                "weight": 0.95,
                "confidence": 85,
                "severity": "critical",
                "raw": raid,
            }
        )

    return nodes


def format_for_prompt(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "服务器硬件画像：未采集。"
    if profile.get("status") != "ok":
        return f"服务器硬件画像：未采集（{profile.get('message') or '不可用'}）。"

    cpu = profile.get("cpu") or {}
    mem = profile.get("memory") or {}
    io = profile.get("io") or {}
    virt = profile.get("virtualization") or {}
    lines = ["服务器硬件画像："]
    cpu_bits = []
    if cpu.get("model_name"):
        cpu_bits.append(str(cpu["model_name"]))
    if cpu.get("logical_cpus"):
        cpu_bits.append(f"{cpu.get('logical_cpus')} 逻辑核")
    if cpu.get("sockets") and cpu.get("cores_per_socket"):
        cpu_bits.append(f"{cpu.get('sockets')} socket x {cpu.get('cores_per_socket')} 核")
    if cpu_bits:
        lines.append(f"- CPU：{'，'.join(cpu_bits)}")
    if mem.get("total_mb"):
        lines.append(
            f"- 内存：{round(_to_float(mem.get('total_mb')) / 1024, 1)}GB，总使用率 {mem.get('used_pct')}%，Swap {mem.get('swap_used_pct', 0)}%"
        )
    if profile.get("filesystems"):
        root = next((item for item in profile["filesystems"] if item.get("mount") == "/"), None)
        top_fs = sorted(
            [item for item in profile["filesystems"] if not (root and item.get("mount") == root.get("mount"))],
            key=lambda item: _to_float(item.get("used_pct")),
            reverse=True,
        )[:3]
        fs_text = "；".join(f"{item.get('mount')} 使用率 {item.get('used_pct')}%" for item in top_fs)
        if root:
            suffix = f"；{fs_text}" if fs_text else ""
            lines.append(f"- 文件系统：根文件系统 / 使用率 {root.get('used_pct')}%{suffix}")
        else:
            lines.append(f"- 文件系统：{fs_text}")
    if io:
        lines.append(f"- IO：最慢磁盘 {io.get('worst_device')} await {io.get('await_ms')}ms，util {io.get('util_pct')}%")
    network = [nic for nic in (profile.get("network") or []) if nic.get("state") == "up"] or (profile.get("network") or [])
    if network:
        net_text = "；".join(
            f"{nic.get('iface')} {nic.get('speed_mbps') if _to_int(nic.get('speed_mbps')) > 0 else 'unknown'}Mbps errors={_to_int(nic.get('rx_errors')) + _to_int(nic.get('tx_errors'))}"
            for nic in network[:4]
        )
        lines.append(f"- 网络：{net_text}")
    asm = ((profile.get("storage") or {}).get("asm") or {})
    if asm.get("detected"):
        groups = asm.get("diskgroups") or []
        if groups:
            lines.append(
                "- ASM："
                + "；".join(
                    f"ASM {dg.get('name')} 使用率 {dg.get('used_pct')}% state={dg.get('state')}" for dg in groups[:4]
                )
            )
        else:
            lines.append("- ASM：检测到 ASM，未采集到磁盘组容量明细")
    raid = ((profile.get("storage") or {}).get("raid") or {})
    if raid.get("detected"):
        lines.append(f"- RAID/多路径：{'降级' if raid.get('degraded') else '检测到'}")
    if virt.get("type"):
        lines.append(f"- 虚拟化：{virt.get('type')}")
    return "\n".join(lines)
