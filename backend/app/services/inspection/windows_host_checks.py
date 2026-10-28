"""Bounded, read-only Windows adaptation of backend/scripts/oscheck.ps1.

No source script is uploaded or executed. Register WINDOWS_HOST_ITEMS in the
Windows policy and dispatch its enabled items here (not to host_overview).
The caller still owns template seeding, platform selection and report export:
host_windows uses host_platform='windows', script_name='oscheck.ps1'; the Unix
host_resource template uses host_platform='unix'. Reject mixed platform items
in template validation before dispatch (this collector ignores foreign keys).
Windows PowerShell 3+ and an existing Windows SSH endpoint are prerequisites.
Local CIM queries use COM/WMI, with no ComputerName/CimSession or WinRM setup.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.analyzers.rules import Finding
from app.services.inspection.custom_metric_service import (
    enabled_metric_source_keys,
    metric_source_key,
)
from app.services.ssh_executor import ssh_connection


CONNECT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 15
COLLECTION_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TOTAL_OUTPUT_BYTES = 512 * 1024
MAX_ROWS = 200
SOURCE_SCRIPT = "oscheck.ps1"
PREFIX = "host.windows."

# Every remote expression is fixed here. Policy commands, SQL and paths are
# metadata only and must never become executable PowerShell.
_QUERIES = {
    "os": "Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop | "
    "Select-Object Caption,Version,BuildNumber,TotalVisibleMemorySize,FreePhysicalMemory,"
    "@{Name='UptimeSeconds';Expression={((Get-Date)-$_.LastBootUpTime).TotalSeconds}}",
    "cpu": "Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | "
    "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,LoadPercentage",
    "pagefile": "Get-CimInstance -ClassName Win32_PageFileUsage -ErrorAction Stop | "
    "Select-Object Name,AllocatedBaseSize,CurrentUsage,PeakUsage",
    "queue": "Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_System "
    "-ErrorAction Stop | Select-Object ProcessorQueueLength",
    "time": "Get-Date -Format o",
    "hosts": r"$p = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'; "
    r"if ($p -notmatch '^[A-Za-z]:\\') { throw 'Non-local hosts path refused' }; "
    "Get-Content -LiteralPath $p -ErrorAction Stop | "
    "Where-Object { $_ -and $_ -notmatch '^\\s*(#|$)' }",
    "registry": r"$tcp = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -ErrorAction Stop; "
    r"$mm = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -ErrorAction Stop; "
    "foreach ($k in @('TcpTimedWaitDelay','MaxUserPort','TcpNumConnections','KeepAliveTime','KeepAliveInterval')) "
    "{ $p = $tcp.PSObject.Properties[$k]; "
    "[pscustomobject]@{Name=$k;Present=($null -ne $p);Value=$(if($null -ne $p){$p.Value}else{$null})} }; "
    "foreach ($k in @('LargeSystemCache','PagingFiles')) { $p = $mm.PSObject.Properties[$k]; "
    "[pscustomobject]@{Name=$k;Present=($null -ne $p);Value=$(if($null -ne $p){$p.Value}else{$null})} }",
    "disk": "Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3' "
    "-ErrorAction Stop | Select-Object DeviceID,FileSystem,VolumeName,Size,FreeSpace",
    "memory_cache": "Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Memory "
    "-ErrorAction Stop | Select-Object CacheBytes",
    "tasks": "Get-ScheduledTask -ErrorAction Stop | "
    "Where-Object { $_.TaskName -match 'oracle|rman|backup|dbcheck' -and $_.State -ne 'Disabled' } | "
    "Select-Object TaskName,TaskPath,@{Name='State';Expression={[string]$_.State}}",
    "services": "Get-CimInstance -ClassName Win32_Service "
    "-Filter \"Name LIKE 'OracleService%' OR Name LIKE 'OracleGICluster%' OR Name LIKE 'CRSService%'\" "
    "-ErrorAction Stop | Select-Object Name,State,StartName",
}


@dataclass(frozen=True)
class _Section:
    key: str
    name: str
    category: str
    queries: tuple[str, ...] = ()
    unsupported: str = ""


_SECTIONS = (
    _Section("os_type", "Windows 操作系统类型", "host_os", ("os",)),
    _Section("rac", "Oracle 集群服务迹象（不代表确认 RAC）", "rac_cluster", ("services",)),
    _Section("osversion", "Windows 操作系统版本", "host_os", ("os",)),
    _Section("kernel", "Windows 内核版本与构建号", "host_os", ("os",)),
    _Section("cpu", "处理器型号与核心数", "host_cpu", ("cpu",)),
    _Section("cpuasge", "CPU 使用率", "host_cpu", ("cpu",)),
    _Section("memtotal", "物理内存容量（GiB）", "host_memory", ("os",)),
    _Section("memusage", "物理内存使用率", "host_memory", ("os",)),
    _Section("swap", "Windows 分页文件分配容量（GiB）", "host_memory", ("pagefile",)),
    _Section("swapusage", "Windows 分页文件使用率", "host_memory", ("pagefile",)),
    _Section("loadaverage", "处理器队列长度（非 Unix 平均负载）", "host_cpu", ("queue",)),
    _Section("upday", "开机运行时长（秒）", "host_os", ("os",)),
    _Section("time", "主机时间与时区偏移", "host_os", ("time",)),
    _Section("hosts", "Windows hosts 有效配置", "host_os", ("hosts",)),
    _Section("sysctl", "TCP 与内存注册表参数（非 sysctl）", "host_kernel", ("registry",)),
    _Section("limits", "用户特权", "host_limits", unsupported="未支持：原脚本使用外部 whoami /priv；尚未实现只读用户特权适配。"),
    _Section("diskusage", "本地固定磁盘使用率", "host_storage", ("disk",)),
    _Section("inode", "Unix inode 使用率", "host_storage", unsupported="不适用：Windows 没有等价的 Unix inode 使用率；原脚本占位值不是真实测量。"),
    _Section("meminfo", "物理内存与分页文件明细", "host_memory", ("os", "pagefile")),
    _Section("freemem", "物理内存、分页文件与缓存", "host_memory", ("os", "pagefile", "memory_cache")),
    _Section("thp", "Linux 透明大页", "host_memory", unsupported="不适用：Windows 不支持 Linux THP；原脚本的 never 占位值不能证明配置正常。"),
    _Section("crontab", "已启用的 Oracle 与备份计划任务", "host_scheduler", ("tasks",)),
    _Section("dbusers", "Oracle 服务账号与运行状态", "oracle_runtime", ("services",)),
    _Section("dbpatch", "Oracle 数据库 Home 补丁", "oracle_patch", unsupported="未支持：依赖外部 OPatch 与 Oracle 版本工具，不在本主机只读适配范围。"),
    _Section("gipatch", "Grid Home 补丁", "oracle_patch", unsupported="未支持：依赖外部 OPatch 与 Grid Home 识别，不在本主机只读适配范围。"),
    _Section("lsnrctl", "Oracle 监听器状态", "oracle_listener", unsupported="未支持：需要外部 lsnrctl；服务存在不能证明监听器健康。"),
    _Section("sqlnet", "Oracle sqlnet.ora 配置", "oracle_listener", unsupported="未支持：尚未实现安全、无歧义的本地 Oracle Home 解析；不接受任意文件路径或远程路径。"),
    _Section("version_route", "Oracle 版本识别与 SQL 路由", "oracle_runtime", unsupported="未支持：本适配器不调用 SQLPlus，不建立数据库连接；版本路由由数据库采集器负责。"),
    _Section("dbreport", "SQLPlus 数据库报告", "report_archive", unsupported="未支持：本适配器不执行外部 SQLPlus 或数据库脚本。"),
    _Section("archive", "原脚本文本与 ZIP 归档", "report_archive", unsupported="未支持：不在远程主机写入、改名、删除或归档文件；平台报告导出由独立流程负责。"),
)
_BY_KEY = {PREFIX + section.key: section for section in _SECTIONS}


def _script(query: str) -> str:
    body = _QUERIES[query]
    return (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "$WarningPreference='Stop'; Set-StrictMode -Version 2.0; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "try { if ([Environment]::OSVersion.Platform -ne 'Win32NT') "
        "{ throw (New-Object System.PlatformNotSupportedException 'Windows required') }; "
        f"$rows=@(& {{ {body} }} | Select-Object -First {MAX_ROWS + 1}); "
        f"if ($rows.Count -gt {MAX_ROWS}) {{ throw 'Row limit exceeded' }}; "
        "@{state='collected';data=$rows;error=$null} | ConvertTo-Json -Depth 8 -Compress "
        "} catch { $state='collection_failed'; "
        "if ($_.Exception -is [System.Management.Automation.CommandNotFoundException] "
        "-or $_.Exception -is [System.PlatformNotSupportedException]) { $state='not_supported' }; "
        "@{state=$state;data=@();error=($_.Exception.Message);error_id=($_.FullyQualifiedErrorId)} "
        "| ConvertTo-Json -Depth 8 -Compress }"
    )


def _command(query: str) -> str:
    payload = base64.b64encode(_script(query).encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + payload


WINDOWS_HOST_ITEMS = [
    {
        "key": PREFIX + section.key,
        "name": section.name,
        "category": section.category,
        "enabled": not bool(section.unsupported),
        "severity": "warning" if section.key in {"cpuasge", "memusage", "swapusage", "diskusage"} else "info",
        "source_script": SOURCE_SCRIPT,
        "source_section": section.key,
        "source_type": "builtin",
        "collect_method": "沿用现有 SSH 的只读 Windows PowerShell 采集",
        "description": section.unsupported or "按原始 oscheck.ps1 对应章节适配的 Windows 只读观察项；不整体执行原脚本，不代表完整等价。",
        "normal_range": "数值按采集器默认阈值判定；配置与清单须人工核对基线",
        "warning_range": "达到预警阈值、能力不支持或采集失败",
        "critical_range": "实测数值达到严重阈值",
        "unit": "状态/数值",
        "advice": section.unsupported or "核对采集证据、Windows 与数据库基线；缺失数据不得视为正常，不通过本采集器修改配置。",
        "command": "\n\n".join(_script(query) for query in section.queries),
        "supported": not bool(section.unsupported),
    }
    for section in _SECTIONS
]


def _run_command(client, command: str, *, timeout: float, max_bytes: int) -> dict:
    """Drain both streams within one byte/deadline budget, including exec ACK.

    ManagedSSHClient.execute_command reads streams without a size bound. Keep
    its connection ownership, but use its Paramiko transport for bounded reads.
    Closing a timed-out SSH channel is not proof the remote process was killed.
    """
    started = time.monotonic()
    channel = None
    timer = None
    output, errors = bytearray(), bytearray()
    result: dict[str, Any] = {"command": command, "exit_code": None,
                              "timeout_seconds": timeout, "output_limit_bytes": max_bytes}
    try:
        transport = client.client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH transport unavailable")
        channel = transport.open_session(timeout=timeout)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Command deadline exceeded")
        channel.settimeout(remaining)
        # Paramiko's exec-request acknowledgement can wait independently of
        # socket timeouts. Closing this channel also releases that wait.
        timer = threading.Timer(remaining, channel.close)
        timer.daemon = True
        timer.start()
        channel.exec_command(command)
        channel.shutdown_write()
        while True:
            if time.monotonic() - started >= timeout:
                raise TimeoutError("Command deadline exceeded")
            received = False
            for ready, read, dest in (
                (channel.recv_ready, channel.recv, output),
                (channel.recv_stderr_ready, channel.recv_stderr, errors),
            ):
                if ready():
                    chunk = read(min(4096, max_bytes - len(output) - len(errors) + 1))
                    received = bool(chunk) or received
                    room = max_bytes - len(output) - len(errors)
                    dest.extend(chunk[:room])
                    if len(chunk) > room:
                        result["output_limited"] = True
                        raise ValueError("Combined stdout/stderr byte limit exceeded")
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                result["exit_code"] = channel.recv_exit_status()
                break
            if channel.closed:
                raise RuntimeError("SSH channel closed without complete result")
            if not received:
                time.sleep(min(0.01, max(0, timeout - (time.monotonic() - started))))
        if time.monotonic() - started >= timeout:
            raise TimeoutError("Command deadline exceeded")
    except Exception as exc:
        result["transport_error"] = type(exc).__name__
        result["error"] = str(exc)[:1000]
        if time.monotonic() - started >= timeout or isinstance(exc, TimeoutError):
            result["timed_out"] = True
    finally:
        if timer is not None:
            timer.cancel()
        if channel is not None:
            channel.close()
    result.update(stdout=output.decode("utf-8", errors="replace"),
                  stderr=errors.decode("utf-8", errors="replace"),
                  output_bytes=len(output) + len(errors),
                  elapsed_seconds=round(time.monotonic() - started, 3))
    return result


def _decode(result: dict) -> dict:
    if result.get("error") or result.get("exit_code") != 0 or result.get("stderr"):
        return {"state": "collection_failed", "error": result.get("error") or "Nonzero exit or stderr; see execution evidence"}
    try:
        record = json.loads(result["stdout"].lstrip("\ufeff"))
        if not isinstance(record, dict) or record.get("state") not in {"collected", "not_supported", "collection_failed"}:
            raise ValueError("Invalid result envelope")
        if not isinstance(record.get("data"), list):
            raise ValueError("Invalid result rows")
        if len(record["data"]) > MAX_ROWS:
            return {"state": "collection_failed", "error": "返回行数超过单查询上限，结果未采纳", "truncated": True}
        if record["state"] != "collected" and not record.get("error"):
            raise ValueError("Failure result is missing its reason")
        if record["state"] == "collected" and record.get("error"):
            raise ValueError("Successful result contains an error")
        # Remote JSON cannot replace locally recorded command/exit evidence.
        return {field: record[field] for field in ("state", "data", "error", "error_id") if field in record}
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return {"state": "collection_failed", "error": f"Invalid JSON result: {exc}"}


def _number(row: dict, field: str, *, positive: bool = False) -> float:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Missing or nonnumeric {field}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError(f"Invalid {field}")
    return number


def _one(rows: list) -> dict:
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Expected exactly one observation")
    return rows[0]


def _pct(used: float, total: float) -> float:
    if total <= 0 or used > total or used < 0:
        raise ValueError("Invalid capacity observation")
    return round(used / total * 100, 2)


def _evaluate(section: _Section, data: dict[str, list]) -> tuple[Any, str, str]:
    """Return value, status and threshold; inventory success is info, not health."""
    key = section.key
    rows = data[section.queries[0]]
    value: Any = rows
    threshold = "仅采集观察值，须与已审批的基线核对"
    status = "info"
    if key in {"os_type", "osversion", "kernel", "memtotal", "memusage", "upday", "meminfo", "freemem"}:
        os = _one(rows)
        if key in {"os_type", "osversion", "kernel"}:
            fields = ("Caption", "Version", "BuildNumber")
            if any(not isinstance(os.get(field), str) or not os[field].strip() for field in fields):
                raise ValueError("Missing OS identity fields")
            value = {field: os[field] for field in fields}
        elif key == "upday":
            value = _number(os, "UptimeSeconds")
        else:
            total = _number(os, "TotalVisibleMemorySize", positive=True)
            free = _number(os, "FreePhysicalMemory")
            usage = _pct(total - free, total)
            value = round(total / 1048576, 2) if key == "memtotal" else usage
            if key in {"meminfo", "freemem"}:
                _pagefile(data["pagefile"])
                value = {"physical_total_kib": total, "physical_free_kib": free,
                         "pagefiles": data["pagefile"]}
                if key == "freemem":
                    value["cache_bytes"] = _number(_one(data["memory_cache"]), "CacheBytes")
    elif key in {"cpu", "cpuasge"}:
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("No processor observations")
        if key == "cpuasge":
            loads = [_number(row, "LoadPercentage") for row in rows]
            if any(load > 100 for load in loads):
                raise ValueError("Invalid CPU percentage")
            value = round(sum(loads) / len(loads), 2)
        elif any(not row.get("Name") for row in rows):
            raise ValueError("Missing processor name")
    elif key in {"swap", "swapusage"}:
        total, used = _pagefile(rows)
        if key == "swapusage" and total == 0:
            return "not_applicable", "info", "未分配分页文件，使用率百分比不适用"
        value = round(total / 1024, 2) if key == "swap" else _pct(used, total)
    elif key == "loadaverage":
        value = _number(_one(rows), "ProcessorQueueLength")
        threshold = "处理器队列长度不等于 Unix 平均负载，不套用平均负载阈值"
    elif key == "diskusage":
        if not rows:
            raise ValueError("No local fixed-disk observations")
        values = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("DeviceID"):
                raise ValueError("Missing disk identity")
            size = _number(row, "Size", positive=True)
            free = _number(row, "FreeSpace")
            values.append(_pct(size - free, size))
        value = max(values)
    elif key in {"rac", "dbusers"}:
        if any(not isinstance(row, dict) or not row.get("Name") or not row.get("State") for row in rows):
            raise ValueError("Incomplete service observations")
        prefix = ("OracleGICluster", "CRSService") if key == "rac" else ("OracleService",)
        value = [row for row in rows if row["Name"].startswith(prefix)]
        threshold = "仅为服务状态证据；未发现服务不代表确认单机架构或数据库健康"
    elif key == "time":
        if len(rows) != 1 or not isinstance(rows[0], str) or not rows[0].strip():
            raise ValueError("Missing host time")
        value = rows[0]
    elif key == "hosts":
        if any(not isinstance(row, str) for row in rows):
            raise ValueError("Invalid hosts entries")
    elif key == "sysctl":
        expected = {"TcpTimedWaitDelay", "MaxUserPort", "TcpNumConnections", "KeepAliveTime", "KeepAliveInterval", "LargeSystemCache", "PagingFiles"}
        if len(rows) != len(expected) or any(not isinstance(row, dict) or not isinstance(row.get("Present"), bool) for row in rows):
            raise ValueError("Incomplete registry observations")
        if {row.get("Name") for row in rows} != expected:
            raise ValueError("Missing registry settings")
    elif key == "crontab":
        if any(not isinstance(row, dict) or not row.get("TaskName") or not row.get("State") for row in rows):
            raise ValueError("Incomplete scheduled task observations")
    if key in {"cpuasge", "memusage", "diskusage", "swapusage"}:
        warning, critical = (30, 60) if key == "swapusage" else (80, 90)
        threshold = f"预警 >= {warning}%；严重 >= {critical}%（采集器默认阈值）"
        status = "critical" if value >= critical else "warning" if value >= warning else "normal"
    return value, status, threshold


def _pagefile(rows: list) -> tuple[float, float]:
    total = used = 0.0
    for row in rows:
        if not isinstance(row, dict) or not row.get("Name"):
            raise ValueError("Missing pagefile identity")
        allocated = _number(row, "AllocatedBaseSize")
        current = _number(row, "CurrentUsage")
        if current > allocated:
            raise ValueError("Invalid pagefile usage")
        total += allocated
        used += current
    return total, used


def _finding(item: dict, section: _Section | None, cache: dict, connection_error: str = "") -> Finding:
    key = metric_source_key(item)
    executions = [{"query": query, **cache.get(query, {
        "executed": False, "state": "collection_failed",
        "error": connection_error or "达到整体采集时限或总输出上限，未执行此查询",
    }), "command": _script(query), "executed_command": _command(query)}
        for query in (section.queries if section else ())]
    state, status, value = "collected", "info", ""
    reason = "已采集观察数据，请结合证据核对基线；不代表自动判定健康"
    threshold = "只读采集成功"
    if section is None or section.unsupported:
        state, status = "not_supported", "warning"
        reason = section.unsupported if section else "未支持：未知 Windows 检查项，没有对应的白名单命令"
    else:
        failure = next((record for record in executions if record.get("state") != "collected"), None)
        if failure:
            state, status = failure.get("state", "collection_failed"), "warning"
            reason = failure.get("error") or "采集失败，请查看执行证据"
        else:
            try:
                value, status, threshold = _evaluate(section, {row["query"]: row["data"] for row in executions})
                if value == "not_applicable":
                    state, reason = "not_applicable", threshold
                elif status in {"normal", "warning", "critical"}:
                    reason = f"实测值 {value}；{threshold}"
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                state, status, reason = "collection_failed", "warning", f"采集数据无效：{exc}"
    if state != "collected":
        value = state
    # Report detail consumes this flat contract, while evidence below retains
    # the original per-query output, errors and actual execution records.
    rows = []
    for entry in executions:
        if not entry.get("executed") or entry.get("state") != "collected":
            continue
        for observation in entry["data"]:
            row = dict(observation) if isinstance(observation, dict) else {"value": observation}
            row["query"] = entry["query"]
            rows.append(row)
    columns = list(dict.fromkeys(["query", *(field for row in rows for field in row)])) if rows else []
    truncated = any(
        entry.get("output_limited") or entry.get("truncated") or entry.get("error") == "Row limit exceeded"
        for entry in executions
    )
    template_item_key = str(item.get("key") or key)
    message = reason
    if state in {"collection_failed", "not_supported"} and rows:
        message = f"本项未完整采集，仅展示已返回的 {len(rows)} 行观察数据。{reason}"
    return Finding(
        check_category=str(item.get("category") or (section.category if section else "host_os")),
        check_item=str(item.get("name") or (section.name if section else key)),
        check_status=status,
        metric_name=template_item_key,
        metric_value=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
        threshold_value=threshold,
        finding=reason,
        suggestion=section.unsupported if section and section.unsupported else "核对 Windows 权限、可用 PowerShell 命令及执行证据；缺失数据不得视为正常。",
        raw_data={"source_script": SOURCE_SCRIPT, "source_section": section.key if section else None,
                  "source_key": key, "state": state, "command": [entry["command"] for entry in executions],
                  "evidence": executions, "reason": reason,
                  "sql": [entry["command"] for entry in executions],
                  "rows": rows, "columns": columns, "row_count": len(rows),
                  "truncated": truncated, "status": status, "read_only": True,
                  "template_item_key": template_item_key, "message": message,
                  "template_threshold_eligible": bool(
                      state == "collected" and section
                      and section.key in {"cpuasge", "memusage", "swapusage", "diskusage"}
                  ),
                  "scope": "Windows 只读适配；不执行外部 Oracle 工具，不写文件、不归档"},
    )


def collect_windows_host_findings(instance, policy_items: list[dict] | None = None) -> list[Finding]:
    """None selects native defaults; [] disables collection; aliases keep identity.

    One existing SSH connection, fixed query allowlist, per-query and aggregate
    budgets. No retry/reconnect and no substitution with host_overview results.
    Unknown Windows keys produce explicit unsupported findings; other collectors'
    keys are ignored. Threshold policy editing remains the caller's integration.
    """
    items = WINDOWS_HOST_ITEMS if policy_items is None else policy_items
    requested = [(item, _BY_KEY.get(metric_source_key(item))) for item in items
                 if metric_source_key(item) in (enabled_metric_source_keys([item]) or set())
                 and metric_source_key(item).startswith(PREFIX)]
    queries = list(dict.fromkeys(query for _, section in requested if section for query in section.queries))
    cache: dict[str, dict] = {}
    connection_error = ""
    if queries:
        deadline = time.monotonic() + COLLECTION_TIMEOUT_SECONDS
        total_bytes = 0
        try:
            with ssh_connection(instance, timeout=CONNECT_TIMEOUT_SECONDS) as client:
                for query in queries:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or total_bytes >= MAX_TOTAL_OUTPUT_BYTES:
                        break
                    execution = _run_command(client, _command(query),
                                             timeout=min(COMMAND_TIMEOUT_SECONDS, remaining),
                                             max_bytes=min(MAX_OUTPUT_BYTES, MAX_TOTAL_OUTPUT_BYTES - total_bytes))
                    total_bytes += execution["output_bytes"]
                    cache[query] = {**execution, **_decode(execution), "executed": True}
        except Exception as exc:
            # Avoid persisting connection exception text containing credentials.
            connection_error = f"SSH 连接不可用（{type(exc).__name__}），请核对已配置的 SSH 凭据与连通性"
    return [_finding(item, section, cache, connection_error) for item, section in requested]
