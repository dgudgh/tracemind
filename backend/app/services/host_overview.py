import json
import logging
import re
from datetime import datetime

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance
from app.services.ssh_executor import ManagedSSHClient, SSHConnectionError

logger = logging.getLogger("app.host_overview")

HOST_RUNTIME_ITEM_KEYS = {
    "host.os.type",
    "host.os.release",
    "host.kernel.version",
    "host.time.current",
    "host.network.hosts",
    "host.cpu.model",
    "host.cpu.used_pct",
    "host.load_average",
    "host.memory.total_gb",
    "host.memory.used_pct",
    "host.swap.total_gb",
    "host.swap.used_pct",
    "host.memory.detail",
    "host.memory.thp_hugepages",
    "host.filesystem.usage",
    "host.filesystem.inode",
    "host.kernel.sysctl",
    "host.user.limits",
    "host.scheduler.crontab",
    "oracle.runtime.dbusers",
    "oracle.runtime.instances",
    "oracle.runtime.version_route",
    "oracle.patch.db_home",
    "oracle.patch.gi_home",
    "oracle.listener.status",
    "oracle.listener.sqlnet",
    "rac.cluster.detect",
    "rac.cluster.ssh_trust",
    "rac.cluster.ocrcheck",
    "rac.cluster.votedisk",
    "rac.cluster.ocrbackup",
    "rac.cluster.oifcfg",
    "rac.cluster.crs_status",
    "rac.cluster.nodes",
    "report.output.oscheck_file",
    "report.output.dbcheck_html",
    "report.output.tarball",
}

SAFE_COMMANDS = {
    "hostname": "hostname",
    "ip": "hostname -I 2>/dev/null || echo ''",
    "os": "cat /etc/os-release 2>/dev/null | head -5",
    "kernel": "uname -r",
    "cpu_count": "nproc",
    "cpu_model": "lscpu 2>/dev/null | grep 'Model name' | head -1",
    "memory": "free -m 2>/dev/null | grep Mem",
    "swap": "free -m 2>/dev/null | grep Swap",
    "uptime": "uptime",
    "load": "cat /proc/loadavg",
    "df": "df -hP 2>/dev/null | grep -v tmpfs | grep -v devtmpfs | grep -v '^Filesystem'",
    "pmon": "ps -ef | grep ora_pmon | grep -v grep | head -3",
    "smon": "ps -ef | grep ora_smon | grep -v grep | head -3",
    "listener": "ps -ef | grep tnslsnr | grep -v grep | head -3",
    "mysqld": "ps -ef | grep mysqld | grep -v grep | head -3",
    "postgres": "ps -ef | grep 'postgres.*postmaster' | grep -v grep | head -3",
    "ports": "ss -lntp 2>/dev/null || netstat -tlnp 2>/dev/null",
    "current_time": "date '+%Y-%m-%d %H:%M:%S %z'",
    "hosts_file": "sed -n '1,120p' /etc/hosts 2>/dev/null",
    "cpu_used": "LC_ALL=C vmstat 1 2 2>/dev/null | tail -1 | awk '{print 100-$15}'",
    "meminfo": "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree|HugePages_Total|HugePages_Free|Hugepagesize):' /proc/meminfo 2>/dev/null",
    "thp": "cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null",
    "df_inode": "df -iP 2>/dev/null | grep -v '^Filesystem'",
    "sysctl": "sysctl vm.swappiness vm.dirty_ratio vm.dirty_background_ratio fs.file-max net.core.somaxconn 2>/dev/null",
    "limits": "ulimit -a 2>/dev/null",
    "crontab": "crontab -l 2>/dev/null",
    "oracle_users": "ps -eo user=,args= 2>/dev/null | awk '/ora_pmon_/ && !/awk/ {print $1}' | sort -u",
    "oracle_version": "if command -v sqlplus >/dev/null 2>&1; then sqlplus -v; else echo NOT_AVAILABLE; fi",
    "db_patch": "if command -v opatch >/dev/null 2>&1; then opatch lspatches 2>/dev/null | head -40; else echo NOT_AVAILABLE; fi",
    "gi_patch": "if command -v crsctl >/dev/null 2>&1 && command -v opatch >/dev/null 2>&1; then opatch lspatches 2>/dev/null | head -40; else echo NOT_AVAILABLE; fi",
    "listener_sqlnet": "if command -v lsnrctl >/dev/null 2>&1; then lsnrctl status 2>/dev/null | head -80; else echo NOT_AVAILABLE; fi",
    "rac_detect": "if command -v olsnodes >/dev/null 2>&1; then olsnodes 2>/dev/null; else echo NOT_AVAILABLE; fi",
    "rac_ssh_trust": "if command -v olsnodes >/dev/null 2>&1; then for node in $(olsnodes 2>/dev/null); do printf '%s=' \"$node\"; timeout 3 ssh -o BatchMode=yes -o ConnectTimeout=2 \"$node\" hostname 2>/dev/null || echo FAILED; done; else echo NOT_AVAILABLE; fi",
    "rac_ocrcheck": "if command -v ocrcheck >/dev/null 2>&1; then ocrcheck 2>/dev/null | head -80; else echo NOT_AVAILABLE; fi",
    "rac_votedisk": "if command -v crsctl >/dev/null 2>&1; then crsctl query css votedisk 2>/dev/null | head -80; else echo NOT_AVAILABLE; fi",
    "rac_ocrbackup": "if command -v ocrconfig >/dev/null 2>&1; then ocrconfig -showbackup 2>/dev/null | head -80; else echo NOT_AVAILABLE; fi",
    "rac_oifcfg": "if command -v oifcfg >/dev/null 2>&1; then oifcfg getif 2>/dev/null | head -80; else echo NOT_AVAILABLE; fi",
    "rac_crs_status": "if command -v crsctl >/dev/null 2>&1; then crsctl stat res -t 2>/dev/null | head -120; else echo NOT_AVAILABLE; fi",
    "rac_nodes": "if command -v olsnodes >/dev/null 2>&1; then olsnodes -n -s -t 2>/dev/null; else echo NOT_AVAILABLE; fi",
}


def _ssh_exec(instance: DBInstance, cmd: str) -> str:
    if not instance.ssh_host:
        raise SSHConnectionError("未配置 SSH")
    pwd = decrypt_password(instance.ssh_password_cipher) if instance.ssh_password_cipher else None
    client = ManagedSSHClient(
        hostname=instance.ssh_host,
        port=instance.ssh_port or 22,
        username=instance.ssh_username,
        password=pwd,
        timeout=10,
    )
    client.connect()
    result = client.execute_command(cmd, timeout=15)
    client.close()
    return (result.get("stdout") or "").strip()


def _run_batch(instance: DBInstance, commands: dict[str, str], *, platform_probe: bool = False,
               require_known_host: bool = False) -> dict[str, str]:
    results = {}
    client = None
    if not instance.ssh_host:
        return results
    try:
        pwd = decrypt_password(instance.ssh_password_cipher) if instance.ssh_password_cipher else None
        client = ManagedSSHClient(
            hostname=instance.ssh_host,
            port=instance.ssh_port or 22,
            username=instance.ssh_username,
            password=pwd,
            timeout=10,
            require_known_host=require_known_host,
        )
        client.connect()
        if platform_probe:
            platform = client.execute_command("uname -s", timeout=10)
            if platform.get("exit_code") == 0 and platform.get("stdout", "").strip():
                results["_platform"] = platform["stdout"].strip()
            else:
                # Windows OpenSSH may use cmd.exe or PowerShell. Avoid POSIX
                # batching there; both identity commands are fixed and read-only.
                name = client.execute_command("hostname", timeout=10)
                if name.get("exit_code") == 0:
                    results["hostname"] = name.get("stdout", "").strip()
                details = client.execute_command(
                    'powershell.exe -NoProfile -NonInteractive -Command '
                    '"$o=Get-CimInstance Win32_OperatingSystem; '
                    '$c=Get-CimInstance Win32_ComputerSystem; '
                    '$p=Get-CimInstance Win32_Processor | Select-Object -First 1; '
                    '@{os_name=$o.Caption;os_version=$o.Version;cpu_count=$c.NumberOfLogicalProcessors;'
                    'cpu_model=$p.Name;memory_total_mb=[math]::Round($c.TotalPhysicalMemory/1MB)} '
                    '| ConvertTo-Json -Compress"', timeout=20,
                )
                if details.get("exit_code") == 0:
                    data = json.loads(details.get("stdout") or "{}")
                    for key in ("os_name", "os_version", "cpu_count", "cpu_model", "memory_total_mb"):
                        if data.get(key) is not None:
                            results[f"_windows_{key}"] = str(data[key])
                return results
        joined = "; ".join(
            f'echo "<<<{k}>>>"; ( {cmd} ) 2>&1 || true'
            for k, cmd in commands.items()
        )
        result = client.execute_command(joined, timeout=60)
        output = result.get("stdout") or ""
        current_key = None
        current_lines = []
        for line in output.split("\n"):
            marker = re.search(r"<<<(\w+)>>>", line)
            if marker:
                if current_key:
                    results[current_key] = "\n".join(current_lines).strip()
                current_key = marker.group(1)
                current_lines = []
                continue
            if current_key:
                current_lines.append(line)
        if current_key:
            results[current_key] = "\n".join(current_lines).strip()
    except Exception as e:
        logger.warning("SSH batch failed: %s", e)
    finally:
        if client is not None:
            client.close()
    return results


def _parse_os_release(text: str) -> tuple[str, str]:
    os_name = ""
    os_version = ""
    for line in text.split("\n"):
        if line.startswith("NAME="):
            os_name = line.split("=", 1)[1].strip('"').strip("'")
        elif line.startswith("VERSION="):
            os_version = line.split("=", 1)[1].strip('"').strip("'")
    return os_name, os_version


def _parse_memory_line(line: str) -> tuple[int, int, int]:
    parts = line.split()
    if len(parts) >= 3:
        return int(parts[1]), int(parts[2]), int(parts[3])
    return 0, 0, 0


def _parse_uptime(text: str) -> str:
    text = text.strip()
    m = re.search(r"up\s+(.*?),\s*\d+\s*user", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"up\s+(.*?)(?:,\s*\d+\s*user|$)", text)
    if m:
        return m.group(1).strip()
    return text


def _parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = text.strip().split()
    if len(parts) >= 3:
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            pass
    return 0, 0, 0


def _parse_df(lines: str) -> list[dict]:
    result = []
    for line in lines.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total = float(parts[1].replace("G", "").replace("T", "000").replace("M", "0.001").replace("K", "0.000001"))
            used = float(parts[2].replace("G", "").replace("T", "000").replace("M", "0.001").replace("K", "0.000001"))
            avail = float(parts[3].replace("G", "").replace("T", "000").replace("M", "0.001").replace("K", "0.000001"))
            pct_str = parts[4].replace("%", "")
            pct = float(pct_str)
        except (ValueError, IndexError):
            continue
        mount = parts[5] if len(parts) >= 6 else parts[-1]
        level = "normal"
        critical_threshold = 90
        warning_threshold = 80
        if mount in ("/arch", "/archive", "/archivelog"):
            critical_threshold = 85
            warning_threshold = 75
        if pct > critical_threshold:
            level = "critical"
        elif pct > warning_threshold:
            level = "warning"
        result.append({
            "filesystem": parts[0],
            "mount_point": mount,
            "total": parts[1],
            "used": parts[2],
            "available": parts[3],
            "used_pct": pct,
            "level": level,
        })
    return result


def _check_processes(output: str, name: str) -> dict:
    running = bool(output and output.strip())
    return {
        "name": name,
        "status": "running" if running else "not_found",
        "level": "normal" if running else "critical",
        "detail": (output[:200] if running else "未检测到进程"),
    }


def _parse_ports(output: str, db_type: str) -> list[dict]:
    target_ports = {22: "SSH"}
    if db_type == "oracle":
        target_ports[1521] = "Oracle Listener"
    elif db_type == "mysql":
        target_ports[3306] = "MySQL"
    elif db_type == "postgresql":
        target_ports[5432] = "PostgreSQL"
    elif db_type == "sqlserver":
        target_ports[1433] = "SQL Server"

    listening = set()
    for line in output.strip().split("\n"):
        for port in target_ports:
            if f":{port}" in line and "LISTEN" in line.upper():
                listening.add(port)

    return [
        {"port": port, "service": svc, "listening": port in listening}
        for port, svc in sorted(target_ports.items())
    ]


def get_discovery_host_overview(instance: DBInstance, *, require_known_host: bool = False) -> dict:
    """One SSH login and fixed, read-only identity commands for a discovered peer."""
    batch = _run_batch(instance, {key: SAFE_COMMANDS[key] for key in (
        "hostname", "ip", "os", "cpu_count", "cpu_model", "memory",
    )}, platform_probe=True, require_known_host=require_known_host)
    if not batch or not batch.get("hostname", "").strip():
        return {"host_basic": None, "error": "SSH 认证或主机信息采集未通过"}
    if batch.get("_windows_os_name"):
        return {"host_basic": {
            "hostname": batch["hostname"].strip(), "os_name": batch["_windows_os_name"],
            "os_version": batch.get("_windows_os_version", ""),
            "cpu_count": int(batch.get("_windows_cpu_count") or 0),
            "cpu_model": batch.get("_windows_cpu_model", ""),
            "memory_total_mb": int(float(batch.get("_windows_memory_total_mb") or 0)),
        }}
    os_name, os_version = _parse_os_release(batch["os"]) if batch.get("os") else (batch.get("_platform") or "未知", "")
    try:
        cpu_count = int(batch.get("cpu_count", "0").strip())
    except ValueError:
        cpu_count = 0
    model = batch.get("cpu_model", "")
    return {"host_basic": {
        "hostname": batch["hostname"].strip(), "os_name": os_name,
        "os_version": os_version, "cpu_count": cpu_count,
        "cpu_model": model.split(":", 1)[-1].strip(),
        "memory_total_mb": _parse_memory_line(batch.get("memory", ""))[0],
    }}


def get_instance_host_overview(instance: DBInstance, *, discovery_only: bool = False,
                               require_known_host: bool = False) -> dict:
    if discovery_only:
        return get_discovery_host_overview(instance, require_known_host=require_known_host)
    result = {
        "instance_id": instance.id,
        "source_db_type": str(
            getattr(instance, "source_db_type", None)
            or getattr(instance, "db_type", "")
            or ""
        ).lower(),
        "ssh_enabled": bool(instance.ssh_host),
        "collect_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host_basic": None,
        "resources": None,
        "filesystems": [],
        "processes": [],
        "ports": [],
        "command_data": {},
        "risks": [],
        "error": None,
    }

    if not instance.ssh_host:
        result["error"] = "当前实例未配置 SSH，仅显示数据库基础信息，无法采集主机信息。"
        return result

    batch = _run_batch(instance, SAFE_COMMANDS, require_known_host=require_known_host)

    if not batch:
        result["error"] = "SSH 连接失败，无法采集主机信息。"
        return result
    result["command_data"] = {
        key: value[:8000]
        for key, value in batch.items()
    }

    hostname = batch.get("hostname", "")
    ip_raw = batch.get("ip", "")
    ip_address = ip_raw.split()[0] if ip_raw.strip() else (instance.ssh_host or "")
    os_name, os_version = _parse_os_release(batch.get("os", ""))
    kernel = batch.get("kernel", "")
    cpu_count = 0
    try:
        cpu_count = int(batch.get("cpu_count", "0").strip())
    except ValueError:
        pass
    cpu_model_line = batch.get("cpu_model", "")
    cpu_model = cpu_model_line.split(":", 1)[1].strip() if ":" in cpu_model_line else cpu_model_line

    mem_line = batch.get("memory", "")
    mem_total, mem_used, _ = _parse_memory_line(mem_line)
    swap_line = batch.get("swap", "")
    swap_total, swap_used, _ = _parse_memory_line(swap_line)

    uptime_str = _parse_uptime(batch.get("uptime", ""))
    load_1m, load_5m, load_15m = _parse_loadavg(batch.get("load", ""))

    mem_pct = round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0
    swap_pct = round(swap_used / swap_total * 100, 1) if swap_total > 0 else 0

    result["host_basic"] = {
        "hostname": hostname,
        "ip_address": ip_address,
        "os_name": os_name,
        "os_version": os_version,
        "kernel_version": kernel,
        "cpu_count": cpu_count,
        "cpu_model": cpu_model,
        "memory_total_mb": mem_total,
        "uptime": uptime_str,
    }

    cpu_used_pct = 0
    try:
        cpu_used_pct = round(float(batch.get("cpu_used", "0").strip()), 1)
    except Exception:
        if cpu_count > 0:
            cpu_used_pct = round(min(load_1m / cpu_count * 100, 100), 1)

    result["resources"] = {
        "cpu_used_pct": cpu_used_pct,
        "memory_used_pct": mem_pct,
        "memory_total_mb": mem_total,
        "memory_used_mb": mem_used,
        "swap_used_pct": swap_pct,
        "swap_total_mb": swap_total,
        "swap_used_mb": swap_used,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
    }

    result["filesystems"] = _parse_df(batch.get("df", ""))

    db_type = result["source_db_type"]
    procs = []
    if db_type == "oracle":
        procs.append(_check_processes(batch.get("pmon", ""), "Oracle PMON"))
        procs.append(_check_processes(batch.get("smon", ""), "Oracle SMON"))
        procs.append(_check_processes(batch.get("listener", ""), "Oracle Listener"))
    elif db_type == "mysql":
        procs.append(_check_processes(batch.get("mysqld", ""), "MySQL mysqld"))
    elif db_type == "postgresql":
        procs.append(_check_processes(batch.get("postgres", ""), "PostgreSQL postmaster"))
    result["processes"] = procs

    result["ports"] = _parse_ports(batch.get("ports", ""), db_type)

    risks = []
    if cpu_used_pct > 90:
        risks.append({"level": "critical", "message": f"CPU 使用率 {cpu_used_pct}%，超过 90% 临界值"})
    elif cpu_used_pct > 80:
        risks.append({"level": "warning", "message": f"CPU 使用率 {cpu_used_pct}%，超过 80% 警告值"})

    if mem_pct > 90:
        risks.append({"level": "critical", "message": f"内存使用率 {mem_pct}%，超过 90% 临界值"})
    elif mem_pct > 80:
        risks.append({"level": "warning", "message": f"内存使用率 {mem_pct}%，超过 80% 警告值"})

    if swap_pct > 60:
        risks.append({"level": "critical", "message": f"Swap 使用率 {swap_pct}%，超过 60% 临界值"})
    elif swap_pct > 30:
        risks.append({"level": "warning", "message": f"Swap 使用率 {swap_pct}%，超过 30% 警告值"})

    if cpu_count > 0 and load_1m > cpu_count * 2:
        risks.append({"level": "critical", "message": f"Load 1m ({load_1m}) 超过 CPU 核数*2 ({cpu_count * 2})"})
    elif cpu_count > 0 and load_1m > cpu_count:
        risks.append({"level": "warning", "message": f"Load 1m ({load_1m}) 超过 CPU 核数 ({cpu_count})"})

    for fs in result["filesystems"]:
        mount = fs.get("mount_point", "")
        pct = fs.get("used_pct", 0)
        level = fs.get("level", "normal")
        if level == "critical":
            risks.append({"level": "critical", "message": f"{mount} 使用率 {pct}%，超过临界值"})
        elif level == "warning":
            risks.append({"level": "warning", "message": f"{mount} 使用率 {pct}%，超过警告值"})

    for proc in procs:
        if proc["level"] == "critical":
            risks.append({"level": "critical", "message": f"{proc['name']} 进程未检测到，数据库可能未正常运行"})

    for port_info in result["ports"]:
        if port_info["listening"] is False and port_info["port"] != 22:
            risks.append({"level": "warning", "message": f"端口 {port_info['port']} ({port_info['service']}) 未监听"})

    result["risks"] = risks
    return result


def _status_by_pct(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "normal"


def _text_state(value: str, *, missing: str = "未采集") -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "info", missing
    if text == "NOT_AVAILABLE":
        return "info", "当前主机未安装或未配置对应命令"
    return "normal", text


def _inode_summary(text: str) -> tuple[float, list[dict]]:
    rows: list[dict] = []
    maximum = 0.0
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[4].endswith("%"):
            continue
        try:
            used_pct = float(parts[4].rstrip("%"))
        except ValueError:
            continue
        maximum = max(maximum, used_pct)
        rows.append(
            {
                "filesystem": parts[0],
                "used_pct": used_pct,
                "mount_point": parts[-1],
            }
        )
    return maximum, rows


def build_template_checks(
    overview: dict,
    policy_items: list[dict] | None,
) -> list[dict]:
    from app.services.inspection.custom_metric_service import metric_source_key

    requested: list[tuple[str, dict]] = []
    for item in policy_items or []:
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {
                "",
                "0",
                "false",
                "off",
                "no",
                "disabled",
                "停用",
            }
        if not enabled:
            continue
        key = metric_source_key(item)
        if key in HOST_RUNTIME_ITEM_KEYS:
            requested.append((key, item))

    basic = overview.get("host_basic") or {}
    resources = overview.get("resources") or {}
    commands = overview.get("command_data") or {}
    source_db_type = overview.get("source_db_type") or ""
    is_oracle = source_db_type == "oracle"
    rac_detected = bool(
        commands.get("rac_detect")
        and commands.get("rac_detect") != "NOT_AVAILABLE"
    )
    filesystems = overview.get("filesystems") or []
    max_fs = max(
        (float(row.get("used_pct") or 0) for row in filesystems),
        default=0.0,
    )
    max_inode, inode_rows = _inode_summary(commands.get("df_inode", ""))
    process_by_name = {
        str(item.get("name") or ""): item
        for item in (overview.get("processes") or [])
    }

    def result(
        key: str,
        status: str,
        value,
        threshold: str,
        finding: str,
        suggestion: str,
        evidence=None,
    ) -> dict:
        return {
            "key": key,
            "status": status,
            "metric_value": value,
            "threshold_value": threshold,
            "finding": finding,
            "suggestion": suggestion,
            "evidence": evidence,
        }

    checks: list[dict] = []
    for key, item in requested:
        name = str(item.get("name") or key)
        check = None
        if overview.get("error"):
            check = result(
                key,
                "warning",
                "collection_failed",
                "采集成功",
                str(overview["error"]),
                "检查 SSH 地址、端口、账号凭据和主机连通性后重新巡检。",
                {"error": overview["error"]},
            )
        elif key == "host.os.type":
            status, value = _text_state(basic.get("os_name"))
            check = result(key, status, value, "可识别", f"操作系统类型：{value}", "纳入主机基线并持续校验版本兼容性。", basic)
        elif key == "host.os.release":
            status, value = _text_state(basic.get("os_version"))
            check = result(key, status, value, "可识别", f"操作系统版本：{value}", "按数据库认证矩阵核对操作系统版本。", basic)
        elif key == "host.kernel.version":
            status, value = _text_state(basic.get("kernel_version"))
            check = result(key, status, value, "可识别", f"内核版本：{value}", "记录内核基线，变更后重新验证数据库兼容性。", basic)
        elif key == "host.time.current":
            status, value = _text_state(commands.get("current_time"))
            check = result(key, status, value, "时间可读取", f"主机时间：{value}", "确保 NTP/Chrony 同步正常。", {"output": value})
        elif key == "host.network.hosts":
            status, value = _text_state(commands.get("hosts_file"))
            rows = [line for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
            check = result(key, status, len(rows), "配置可读取", f"/etc/hosts 有效配置 {len(rows)} 行", "核对集群节点、VIP、SCAN 和业务域名解析。", {"rows": rows})
        elif key == "host.cpu.model":
            status, value = _text_state(basic.get("cpu_model"))
            check = result(key, status, value, "可识别", f"CPU：{value}，{basic.get('cpu_count') or 0} 核", "记录硬件画像并与授权和容量规划核对。", basic)
        elif key == "host.cpu.used_pct":
            value = float(resources.get("cpu_used_pct") or 0)
            status = _status_by_pct(value, 80, 90)
            check = result(key, status, value, "预警 80%，严重 90%", f"CPU 使用率 {value:.1f}%", "高使用率时结合负载、进程和数据库会话定位来源。", resources)
        elif key == "host.load_average":
            value = float(resources.get("load_1m") or 0)
            cpu_count = int(basic.get("cpu_count") or 0)
            status = "normal"
            if cpu_count and value >= cpu_count * 2:
                status = "critical"
            elif cpu_count and value >= cpu_count:
                status = "warning"
            check = result(key, status, value, "预警 CPU 核数，严重 CPU 核数×2", f"1 分钟负载 {value:g}", "结合运行队列、IO wait 和数据库并发排查负载。", resources)
        elif key == "host.memory.total_gb":
            value = round(float(basic.get("memory_total_mb") or 0) / 1024, 2)
            check = result(key, "normal" if value else "info", value, "> 0 GB", f"物理内存 {value:g} GB", "与数据库 SGA/PGA、Buffer Pool 或 shared_buffers 配置核对。", basic)
        elif key == "host.memory.used_pct":
            value = float(resources.get("memory_used_pct") or 0)
            status = _status_by_pct(value, 80, 90)
            check = result(key, status, value, "预警 80%，严重 90%", f"内存使用率 {value:.1f}%", "检查数据库内存、Page Cache 和异常进程占用。", resources)
        elif key == "host.swap.total_gb":
            value = round(float(resources.get("swap_total_mb") or 0) / 1024, 2)
            check = result(key, "normal", value, "按主机基线", f"Swap 容量 {value:g} GB", "结合数据库厂商建议确认 Swap 容量。", resources)
        elif key == "host.swap.used_pct":
            value = float(resources.get("swap_used_pct") or 0)
            status = _status_by_pct(value, 30, 60)
            check = result(key, status, value, "预警 30%，严重 60%", f"Swap 使用率 {value:.1f}%", "持续换页时检查内存压力和数据库内存配置。", resources)
        elif key == "host.memory.detail":
            status, value = _text_state(commands.get("meminfo"))
            check = result(key, status, len(value.splitlines()), "关键项可读取", "已采集 /proc/meminfo 关键项", "结合 MemAvailable、Swap 和 HugePages 评估内存压力。", {"output": value})
        elif key == "host.memory.thp_hugepages":
            status, value = _text_state(commands.get("thp"))
            if "[always]" in value:
                status = "warning"
            check = result(key, status, value, "数据库主机通常禁用 THP", f"THP 状态：{value}", "按数据库厂商建议配置 THP 和 HugePages。", {"thp": value, "meminfo": commands.get("meminfo")})
        elif key == "host.filesystem.usage":
            status = _status_by_pct(max_fs, 80, 90)
            check = result(key, status, max_fs, "预警 80%，严重 90%", f"文件系统最高使用率 {max_fs:.1f}%", "清理无效文件或扩容高使用率挂载点。", filesystems)
        elif key == "host.filesystem.inode":
            status = _status_by_pct(max_inode, 80, 90)
            check = result(key, status, max_inode, "预警 80%，严重 90%", f"inode 最高使用率 {max_inode:.1f}%", "定位小文件增长来源并清理或扩容。", inode_rows)
        elif key == "host.kernel.sysctl":
            status, value = _text_state(commands.get("sysctl"))
            check = result(key, status, len(value.splitlines()), "关键参数可读取", "已采集关键 sysctl 参数", "与数据库主机基线比对并通过审批变更。", {"output": value})
        elif key == "host.user.limits":
            status, value = _text_state(commands.get("limits"))
            check = result(key, status, len(value.splitlines()), "limits 可读取", "已采集当前 SSH 用户 limits", "核对数据库运行用户 nofile、nproc 和 stack 限制。", {"output": value})
        elif key == "host.scheduler.crontab":
            status, value = _text_state(commands.get("crontab"), missing="当前用户无 crontab")
            rows = [line for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
            check = result(key, status, len(rows), "任务可审计", f"当前用户计划任务 {len(rows)} 条", "核对备份、归档清理和统计信息任务是否按计划运行。", {"rows": rows})
        elif key == "oracle.runtime.dbusers":
            value = commands.get("oracle_users", "")
            status = "normal" if value else ("info" if not is_oracle else "warning")
            check = result(key, status, value or "未识别", "Oracle 主机应识别软件用户", f"Oracle 软件用户：{value or '未识别'}", "确认 Oracle 软件用户和目录属主。", {"output": value})
        elif key == "oracle.runtime.instances":
            value = commands.get("pmon", "")
            status = "normal" if value else ("info" if not is_oracle else "critical")
            check = result(key, status, len(value.splitlines()) if value else 0, "Oracle 实例应有 PMON", "已识别运行实例" if value else "未识别 Oracle PMON", "Oracle 主机未识别 PMON 时检查实例状态。", {"output": value})
        elif key == "oracle.runtime.version_route":
            status, value = _text_state(commands.get("oracle_version"))
            check = result(key, status, value, "版本可识别", f"Oracle 客户端版本：{value}", "按数据库版本选择匹配的 DBCheck SQL。", {"output": value})
        elif key in {"oracle.patch.db_home", "oracle.patch.gi_home"}:
            command_key = "db_patch" if key.endswith("db_home") else "gi_patch"
            status, value = _text_state(commands.get(command_key))
            check = result(key, status, value, "补丁清单可读取", f"{name}：{'已采集' if status == 'normal' else value}", "与季度补丁基线核对并保留回退方案。", {"output": value})
        elif key == "oracle.listener.status":
            process = process_by_name.get("Oracle Listener") or {}
            status = process.get("level") or ("info" if not is_oracle else "critical")
            value = process.get("status") or "not_applicable"
            check = result(key, status, value, "Oracle 主机监听器运行", process.get("detail") or "非 Oracle 主机不适用", "监听异常时检查 listener.ora、服务注册和端口。", process)
        elif key == "oracle.listener.sqlnet":
            status, value = _text_state(commands.get("listener_sqlnet"))
            check = result(key, status, value, "监听配置可读取", f"监听配置采集：{'成功' if status == 'normal' else value}", "核对认证、加密、超时和服务注册配置。", {"output": value})
        elif key == "rac.cluster.detect":
            check = result(key, "normal" if rac_detected else "info", "rac" if rac_detected else "standalone", "自动识别", "检测到 RAC" if rac_detected else "未检测到 RAC，按单机处理", "架构变化后重新执行主机巡检。", {"output": commands.get("rac_detect")})
        elif key == "rac.cluster.ssh_trust":
            value = commands.get("rac_ssh_trust", "NOT_AVAILABLE")
            status = "warning" if rac_detected and "FAILED" in value else ("normal" if rac_detected else "info")
            check = result(key, status, value, "RAC 节点免密 SSH 可用", "RAC SSH 互信已检查" if rac_detected else "非 RAC 主机不适用", "存在 FAILED 时修复节点互信后重新检查。", {"output": value})
        elif key.startswith("rac.cluster."):
            command_key = {
                "rac.cluster.ocrcheck": "rac_ocrcheck",
                "rac.cluster.votedisk": "rac_votedisk",
                "rac.cluster.ocrbackup": "rac_ocrbackup",
                "rac.cluster.oifcfg": "rac_oifcfg",
                "rac.cluster.crs_status": "rac_crs_status",
                "rac.cluster.nodes": "rac_nodes",
            }.get(key)
            value = commands.get(command_key or "", "NOT_AVAILABLE")
            status = "normal" if rac_detected and value != "NOT_AVAILABLE" else ("warning" if rac_detected else "info")
            check = result(key, status, value, "RAC 环境命令执行成功", f"{name}：{'已采集' if status == 'normal' else '非 RAC 主机不适用' if not rac_detected else value}", "RAC 环境采集失败时检查 Grid 用户权限和 CRS 状态。", {"output": value})
        elif key.startswith("report.output."):
            format_name = {
                "report.output.oscheck_file": "主机巡检文本",
                "report.output.dbcheck_html": "数据库巡检 HTML",
                "report.output.tarball": "巡检归档包",
            }.get(key, "巡检报告")
            check = result(key, "info", "generated_by_platform", "任务完成后生成", f"{format_name}由平台报告流程生成", "在巡检报告页导出并校验归档内容。", {"output_type": format_name})

        if check is None:
            check = result(
                key,
                "warning",
                "unmapped",
                "采集器已绑定",
                f"检查项 {name} 尚未绑定主机采集结果",
                "修正模板采集源后重新执行。",
            )
        check["name"] = name
        checks.append(check)
    return checks
