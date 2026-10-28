"""Offline contract and transport tests. No database or remote host is used."""

import base64
from contextlib import contextmanager
import json
from pathlib import Path
import re
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.inspection import windows_host_checks as win


def item(section, **extra):
    return {"key": win.PREFIX + section, **extra}


def envelope(data, state="collected", error=None):
    return json.dumps({"state": state, "data": data, "error": error}).encode()


class Channel:
    def __init__(self, stdout=b"", stderr=b"", exit_code=0, stalled=False, block_exec=False):
        self.stdout = bytearray(stdout)
        self.stderr = bytearray(stderr)
        self.exit_code = exit_code
        self.stalled = stalled
        self.block_exec = block_exec
        self.closed = False
        self.close_event = threading.Event()
        self.commands = []
        self.read_sizes = []

    def settimeout(self, timeout):
        self.timeout = timeout

    def exec_command(self, command):
        self.commands.append(command)
        if self.block_exec:
            assert self.close_event.wait(1), "exec ACK was not interrupted by deadline"
            raise RuntimeError("Channel closed")

    def shutdown_write(self):
        self.stdin_closed = True

    def recv_ready(self):
        return bool(self.stdout)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def _read(self, source, count):
        self.read_sizes.append(count)
        chunk = bytes(source[:count])
        del source[:count]
        return chunk

    def recv(self, count):
        return self._read(self.stdout, count)

    def recv_stderr(self, count):
        return self._read(self.stderr, count)

    def exit_status_ready(self):
        return not self.stalled

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True
        self.close_event.set()


class Transport:
    def __init__(self, channels):
        self.channels = list(channels)
        self.opened = []

    def is_active(self):
        return True

    def open_session(self, timeout):
        channel = self.channels.pop(0)
        self.opened.append(channel)
        return channel


def client_for(channels):
    transport = Transport(channels)
    client = SimpleNamespace(client=SimpleNamespace(get_transport=lambda: transport))
    return client, transport


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tests must not connect to any remote host")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(win, "ssh_connection", denied)


@pytest.fixture
def connect(monkeypatch):
    def install(channels):
        client, transport = client_for(channels)
        calls = []

        @contextmanager
        def connection(instance, timeout):
            calls.append((instance, timeout))
            try:
                yield client
            finally:
                client.closed = True

        monkeypatch.setattr(win, "ssh_connection", connection)
        return client, transport, calls

    return install


@pytest.fixture
def observations():
    return {
        "os": [{"Caption": "Microsoft Windows Server", "Version": "10.0", "BuildNumber": "20348",
                "TotalVisibleMemorySize": 1048576, "FreePhysicalMemory": 524288, "UptimeSeconds": 3600}],
        "cpu": [{"Name": "CPU", "NumberOfCores": 4, "NumberOfLogicalProcessors": 8, "LoadPercentage": 42}],
        "pagefile": [{"Name": "C:\\pagefile.sys", "AllocatedBaseSize": 1024, "CurrentUsage": 128}],
        "queue": [{"ProcessorQueueLength": 3}],
        "time": ["2026-09-08T12:00:00.0000000+08:00"],
        "hosts": ["127.0.0.1 localhost"],
        "registry": [{"Name": name, "Present": False, "Value": None} for name in
                     ["TcpTimedWaitDelay", "MaxUserPort", "TcpNumConnections", "KeepAliveTime", "KeepAliveInterval", "LargeSystemCache", "PagingFiles"]],
        "disk": [{"DeviceID": "C:", "FileSystem": "NTFS", "Size": 1000, "FreeSpace": 100}],
        "memory_cache": [{"CacheBytes": 4096}],
        "tasks": [],
        "services": [{"Name": "OracleServiceXE", "State": "Stopped", "StartName": "LocalSystem"}],
    }


def test_native_items_trace_actual_source_and_do_not_execute_it():
    path = Path(__file__).resolve().parents[1] / "scripts/oscheck.ps1"
    source = path.read_text(encoding="utf-8-sig")
    native_sections = set(re.findall(r'AppendLine\("([a-z]+)"\)', source))
    assert native_sections <= {entry["source_section"] for entry in win.WINDOWS_HOST_ITEMS}
    assert len(win.WINDOWS_HOST_ITEMS) == len({entry["key"] for entry in win.WINDOWS_HOST_ITEMS})
    assert all({"key", "name", "category", "enabled", "severity", "command", "source_section"} <= entry.keys()
               for entry in win.WINDOWS_HOST_ITEMS)
    assert len(win.WINDOWS_HOST_ITEMS) == 30
    assert sum(entry["supported"] for entry in win.WINDOWS_HOST_ITEMS) == 20
    assert sum(entry["enabled"] for entry in win.WINDOWS_HOST_ITEMS) == 20
    for entry in win.WINDOWS_HOST_ITEMS:
        assert entry["enabled"] is entry["supported"]
        for field in ("name", "description", "advice", "collect_method", "normal_range", "warning_range", "critical_range"):
            assert re.search(r"[\u4e00-\u9fff]", entry[field]), (entry["key"], field)
        if entry["supported"]:
            assert "ConvertTo-Json" in entry["command"]
            assert "-EncodedCommand" not in entry["command"]
        else:
            assert entry["command"] == ""
    for query in win._QUERIES:
        command = win._command(query)
        assert command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ")
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        assert script == win._script(query)
        assert "ConvertTo-Json" in script
        assert "-ErrorAction Stop" in script or query == "time"
        assert "Win32NT" in script and "Row limit exceeded" in script
        assert script.index("Win32NT") < script.index(win._QUERIES[query])
        assert len(command) < 8191
        assert not re.search(r"Rename-Item|Remove-Item|New-Item|Move-Item|Compress-Archive|Out-File|WriteAllText|"
                             r"AppendAllText|Set-ItemProperty|Set-ExecutionPolicy|ExecutionPolicy|Invoke-Expression|"
                             r"Invoke-Command|Invoke-CimMethod|sqlplus|opatch|lsnrctl|whoami|"
                             r"-ComputerName|-CimSession|Enable-PSRemoting", script, re.I)


def test_default_inventory_single_connection_shared_queries(connect, observations):
    queries = list(dict.fromkeys(query for section in win._SECTIONS for query in section.queries))
    channels = [Channel(envelope(observations[query])) for query in queries]
    client, transport, calls = connect(channels)
    instance = object()
    findings = win.collect_windows_host_findings(instance, None)
    assert len(findings) == sum(entry["enabled"] for entry in win.WINDOWS_HOST_ITEMS) == 20
    assert calls == [(instance, win.CONNECT_TIMEOUT_SECONDS)]
    assert len(transport.opened) == len(queries)
    assert all(channel.closed and channel.stdin_closed for channel in channels)
    assert client.closed
    by_key = {finding.metric_name.removeprefix(win.PREFIX): finding for finding in findings}
    assert by_key["cpuasge"].check_status == "normal"
    assert by_key["diskusage"].check_status == "critical"
    assert by_key["loadaverage"].metric_value == "3.0"
    assert by_key["loadaverage"].check_status == "info"
    assert by_key["rac"].check_status == "info"
    assert "不代表确认单机架构" in by_key["rac"].threshold_value
    assert by_key["sysctl"].check_status == "info"
    assert by_key["crontab"].metric_value == "[]"
    for finding in findings:
        assert finding.raw_data["source_script"] == "oscheck.ps1"
        payload = finding.raw_data
        assert {"sql", "rows", "columns", "status", "row_count", "truncated", "read_only",
                "template_item_key", "source_key", "template_threshold_eligible", "message"} <= payload.keys()
        assert payload["row_count"] == len(payload["rows"])
        assert payload["read_only"] is True and payload["truncated"] is False
        assert payload["status"] == finding.check_status
        assert payload["template_item_key"] == payload["source_key"] == finding.metric_name
        assert payload["template_threshold_eligible"] is (payload["source_section"] in {
            "cpuasge", "memusage", "swapusage", "diskusage",
        })
        if by_key[finding.raw_data["source_section"]].raw_data["state"] == "collected":
            assert finding.raw_data["command"]
            assert all(entry["executed"] and entry["exit_code"] == 0 and entry["command"]
                       and entry["executed_command"].startswith("powershell.exe ")
                       for entry in finding.raw_data["evidence"])
        else:
            assert finding.metric_value == "not_supported"
            assert finding.check_status == "warning"
            assert finding.raw_data["command"] == []


@pytest.mark.parametrize("enabled", [False, "false", "off", "0", "disabled", "", "停用"])
def test_disabled_and_foreign_items_never_connect(enabled):
    assert win.collect_windows_host_findings(None, [item("time", enabled=enabled), {"key": "mysql.native.x"}]) == []
    assert win.collect_windows_host_findings(None, []) == []


def test_alias_identity_and_untrusted_commands_cannot_change_execution(connect, observations):
    channel = Channel(envelope(observations["cpu"]))
    connect([channel])
    alias = {"key": "copied_cpu", "source_key": win.PREFIX + "cpuasge", "name": "Copied CPU",
             "command": "Remove-Item C:\\*", "sql": "evil", "path": "\\\\otherhost\\share"}
    findings = win.collect_windows_host_findings(None, [alias, item("cpuasge", enabled=False)])
    assert len(findings) == 1
    assert findings[0].metric_name == "copied_cpu"
    assert findings[0].check_item == "Copied CPU"
    assert channel.commands == [win._command("cpu")]
    assert findings[0].raw_data["template_item_key"] == "copied_cpu"
    assert findings[0].raw_data["source_key"] == win.PREFIX + "cpuasge"
    assert findings[0].raw_data["template_threshold_eligible"] is True
    assert findings[0].raw_data["sql"] == [win._script("cpu")]


def test_unsupported_and_unknown_do_not_connect():
    findings = win.collect_windows_host_findings(None, [item("thp"), item("inode"), item("missing")])
    assert all(f.metric_value == "not_supported" and f.check_status == "warning" for f in findings)
    assert all(f.raw_data["evidence"] == [] and f.finding for f in findings)
    for finding in findings:
        assert finding.raw_data["rows"] == finding.raw_data["columns"] == finding.raw_data["sql"] == []
        assert finding.raw_data["row_count"] == 0 and finding.raw_data["status"] == "warning"
        assert finding.raw_data["message"] == finding.raw_data["reason"] == finding.finding
        assert finding.raw_data["template_threshold_eligible"] is False


def test_connection_failure_explicit_per_item_and_no_secret(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("secret-password")

    monkeypatch.setattr(win, "ssh_connection", fail)
    findings = win.collect_windows_host_findings(None, [item("time"), item("cpuasge"), item("thp")])
    assert [f.metric_value for f in findings] == ["collection_failed", "collection_failed", "not_supported"]
    assert all(f.check_status == "warning" for f in findings)
    assert "secret-password" not in repr(findings)
    assert not findings[0].raw_data["evidence"][0]["executed"]
    assert findings[0].raw_data["rows"] == []
    assert findings[0].raw_data["row_count"] == 0
    assert findings[0].raw_data["status"] == "warning"
    assert "SSH" in findings[0].raw_data["message"]
    assert findings[0].raw_data["template_threshold_eligible"] is False


@pytest.mark.parametrize("channel", [
    Channel(b"not json"), Channel(b""), Channel(b"[]"),
    Channel(envelope([], state="other")),
    Channel(json.dumps({"state": "collected", "data": {}}).encode()),
    Channel(envelope([], error="permission denied")),
    Channel(envelope([], state="collection_failed")),
    Channel(envelope(["now"]), exit_code=1),
    Channel(envelope(["now"]), stderr=b"permission denied"),
    Channel(envelope(["now"] * 201)),
])
def test_bad_output_cannot_be_normal(connect, channel):
    connect([channel])
    finding = win.collect_windows_host_findings(None, [item("time")])[0]
    assert finding.metric_value == "collection_failed"
    assert finding.check_status == "warning"
    assert finding.raw_data["evidence"][0]["command"] == win._script("time")
    assert finding.raw_data["evidence"][0]["executed_command"] == win._command("time")


def test_missing_cmdlet_and_permission_failure_stay_distinct(connect):
    channels = [Channel(envelope([], "not_supported", "Get-ScheduledTask not installed")),
                Channel(envelope([], "collection_failed", "Access denied"))]
    connect(channels)
    findings = win.collect_windows_host_findings(None, [item("crontab"), item("sysctl")])
    assert [f.metric_value for f in findings] == ["not_supported", "collection_failed"]
    assert all(f.check_status == "warning" for f in findings)


def test_non_windows_platform_cannot_be_normal(connect):
    connect([Channel(envelope([], "not_supported", "Windows required"))])
    findings = win.collect_windows_host_findings(None, [item("os_type"), item("memusage")])
    assert all(f.metric_value == "not_supported" and f.check_status == "warning" for f in findings)
    assert all(f.finding == "Windows required" for f in findings)


def test_remote_json_cannot_replace_local_command_evidence(connect):
    payload = {"state": "collected", "data": ["2026-09-08T12:00:00+08:00"], "error": None,
               "command": "forged command", "exit_code": 99, "executed": False, "stdout": "forged"}
    connect([Channel(json.dumps(payload).encode())])
    finding = win.collect_windows_host_findings(None, [item("time")])[0]
    evidence = finding.raw_data["evidence"][0]
    assert evidence["command"] == win._script("time")
    assert evidence["executed_command"] == win._command("time")
    assert evidence["exit_code"] == 0 and evidence["executed"]
    assert evidence["stdout"] != "forged"


@pytest.mark.parametrize("row", [{}, {"LoadPercentage": None}, {"LoadPercentage": -1},
                                {"LoadPercentage": 101}, {"LoadPercentage": True},
                                {"LoadPercentage": "0"}, {"LoadPercentage": float("nan")}])
def test_missing_or_invalid_cpu_not_zero(connect, row):
    connect([Channel(envelope([row]))])
    finding = win.collect_windows_host_findings(None, [item("cpuasge")])[0]
    assert finding.metric_value == "collection_failed"
    assert finding.check_status == "warning"


@pytest.mark.parametrize("value,status", [(0, "normal"), (79, "normal"), (80, "warning"), (90, "critical")])
def test_measured_cpu_threshold_boundaries(connect, value, status):
    connect([Channel(envelope([{"LoadPercentage": value}]))])
    assert win.collect_windows_host_findings(None, [item("cpuasge")])[0].check_status == status


def test_empty_pagefile_not_fabricated_zero_usage(connect):
    connect([Channel(envelope([]))])
    findings = win.collect_windows_host_findings(None, [item("swap"), item("swapusage")])
    assert findings[0].metric_value == "0.0" and findings[0].check_status == "info"
    assert findings[1].metric_value == "not_applicable" and findings[1].check_status == "info"
    assert findings[1].raw_data["template_threshold_eligible"] is False
    assert findings[1].raw_data["row_count"] == 0
    assert "未分配分页文件" in findings[1].raw_data["message"]


@pytest.mark.parametrize("section,query,data", [
    ("memusage", "os", [{"TotalVisibleMemorySize": 0, "FreePhysicalMemory": 0}]),
    ("memusage", "os", [{"TotalVisibleMemorySize": 100, "FreePhysicalMemory": 101}]),
    ("diskusage", "disk", []),
    ("diskusage", "disk", [{"DeviceID": "C:", "Size": 0, "FreeSpace": 0}]),
    ("swapusage", "pagefile", [{"Name": "C:", "AllocatedBaseSize": 100, "CurrentUsage": 101}]),
    ("loadaverage", "queue", [{}]),
    ("osversion", "os", [{}]),
    ("sysctl", "registry", []),
])
def test_missing_or_impossible_measurements_fail(connect, section, query, data):
    connect([Channel(envelope(data))])
    finding = win.collect_windows_host_findings(None, [item(section)])[0]
    assert finding.metric_value == "collection_failed"


def test_one_query_failure_does_not_hide_other_evidence(connect, observations):
    connect([Channel(envelope(observations["os"])), Channel(envelope([], "collection_failed", "No permission")),
             Channel(envelope(observations["time"]))])
    findings = win.collect_windows_host_findings(None, [item("meminfo"), item("memusage"), item("time")])
    assert [f.raw_data["state"] for f in findings] == ["collection_failed", "collected", "collected"]
    assert len(findings[0].raw_data["evidence"]) == 2


@pytest.mark.parametrize("stdout,stderr", [(b"x" * 100, b""), (b"", b"x" * 100), (b"a" * 30, b"b" * 40)])
def test_transport_combined_output_limit(stdout, stderr):
    channel = Channel(stdout, stderr)
    client, _ = client_for([channel])
    result = win._run_command(client, "fixed", timeout=1, max_bytes=64)
    assert result["output_limited"]
    assert result["output_bytes"] == 64
    assert channel.closed
    assert all(size > 0 for size in channel.read_sizes)
    assert win._decode(result)["state"] == "collection_failed"


def test_transport_exact_limit_drains_both_streams():
    channel = Channel(b"abc", b"def")
    client, _ = client_for([channel])
    result = win._run_command(client, "fixed", timeout=1, max_bytes=6)
    assert "output_limited" not in result
    assert result["stdout"] == "abc" and result["stderr"] == "def"
    assert result["exit_code"] == 0 and channel.closed


@pytest.mark.parametrize("block_exec", [False, True])
def test_transport_deadline_closes_channel_even_if_exec_ack_hangs(block_exec):
    channel = Channel(stalled=True, block_exec=block_exec)
    client, _ = client_for([channel])
    result = win._run_command(client, "fixed", timeout=0.03, max_bytes=64)
    assert result["timed_out"] and channel.closed
    assert win._decode(result)["state"] == "collection_failed"


def test_aggregate_output_cap_skips_remaining_without_success(connect, monkeypatch):
    monkeypatch.setattr(win, "MAX_TOTAL_OUTPUT_BYTES", 10)
    _, transport, _ = connect([Channel(b"x" * 11)])
    findings = win.collect_windows_host_findings(None, [item("time"), item("cpuasge")])
    assert all(f.metric_value == "collection_failed" for f in findings)
    assert len(transport.opened) == 1
    assert not findings[1].raw_data["evidence"][0]["executed"]


def test_aggregate_deadline_skips_queries(connect, monkeypatch):
    monkeypatch.setattr(win, "COLLECTION_TIMEOUT_SECONDS", 0)
    _, transport, _ = connect([])
    finding = win.collect_windows_host_findings(None, [item("time")])[0]
    assert not transport.opened
    assert finding.metric_value == "collection_failed"


def test_reuses_existing_ssh_credentials_without_new_auth_path(monkeypatch):
    from app.services import ssh_executor

    channel = Channel(envelope(["2026-09-08T12:00:00+08:00"]))
    transport = Transport([channel])
    raw_client = Mock()
    raw_client.get_transport.return_value = transport
    paramiko = SimpleNamespace(SSHClient=lambda: raw_client, AutoAddPolicy=lambda: object())
    decrypt = Mock(return_value="decrypted-password")
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: paramiko)
    monkeypatch.setattr(ssh_executor, "decrypt_password", decrypt)
    monkeypatch.setattr(win, "ssh_connection", ssh_executor.ssh_connection)
    instance = SimpleNamespace(ssh_host="mock-only", ssh_username="existing-user", ssh_port=2222,
                               ssh_password_cipher="cipher")
    finding = win.collect_windows_host_findings(instance, [item("time")])[0]
    decrypt.assert_called_once_with("cipher")
    raw_client.connect.assert_called_once_with(hostname="mock-only", port=2222, username="existing-user",
                                              password="decrypted-password", timeout=10, banner_timeout=10,
                                              auth_timeout=10, allow_agent=False, look_for_keys=False)
    raw_client.close.assert_called_once()
    assert finding.raw_data["state"] == "collected"
    assert "decrypted-password" not in repr(finding)


def test_standard_evidence_expands_multiple_queries_without_losing_identity(connect, observations):
    queries = ("os", "pagefile", "memory_cache")
    connect([Channel(envelope(observations[query])) for query in queries])
    finding = win.collect_windows_host_findings(None, [item("freemem")])[0]
    payload = finding.raw_data
    assert payload["sql"] == payload["command"] == [win._script(query) for query in queries]
    assert payload["rows"] == [{**row, "query": query} for query in queries for row in observations[query]]
    assert payload["row_count"] == 3
    assert payload["columns"][0] == "query"
    assert set(payload["columns"]) == {field for row in payload["rows"] for field in row}
    assert [entry["data"] for entry in payload["evidence"]] == [observations[query] for query in queries]
    assert payload["template_threshold_eligible"] is False
    assert payload["status"] == "info" and payload["state"] == "collected"


@pytest.mark.parametrize("section,query,rows", [
    ("time", "time", ["2026-09-08T12:00:00+08:00"]),
    ("hosts", "hosts", ["127.0.0.1 localhost", "::1 localhost"]),
])
def test_standard_evidence_scalar_observations_have_value_column(connect, section, query, rows):
    connect([Channel(envelope(rows))])
    payload = win.collect_windows_host_findings(None, [item(section)])[0].raw_data
    assert payload["rows"] == [{"query": query, "value": row} for row in rows]
    assert payload["columns"] == ["query", "value"]
    assert payload["row_count"] == len(rows)


def test_partial_failure_only_displays_collected_query_data(connect, observations):
    connect([Channel(envelope(observations["os"])),
             Channel(envelope(observations["pagefile"], "collection_failed", "Access denied"))])
    payload = win.collect_windows_host_findings(None, [item("meminfo")])[0].raw_data
    assert payload["rows"] == [{**row, "query": "os"} for row in observations["os"]]
    assert payload["row_count"] == 1 and payload["status"] == "warning"
    assert payload["state"] == "collection_failed"
    assert "未完整采集" in payload["message"] and "Access denied" in payload["message"]
    assert payload["evidence"][1]["data"] == observations["pagefile"]
    assert payload["template_threshold_eligible"] is False


@pytest.mark.parametrize("stdout,stderr,exit_code", [
    (b"bad json", b"", 0),
    (envelope(["not accepted"]), b"Access denied", 0),
    (envelope(["not accepted"]), b"", 1),
    (envelope(["not accepted"], "collection_failed", "Query failed"), b"", 0),
    (envelope([], "not_supported", "Windows required"), b"", 0),
])
def test_failure_report_has_message_not_fabricated_observation(connect, stdout, stderr, exit_code):
    connect([Channel(stdout, stderr, exit_code)])
    finding = win.collect_windows_host_findings(None, [item("time")])[0]
    payload = finding.raw_data
    assert payload["rows"] == payload["columns"] == [] and payload["row_count"] == 0
    assert payload["sql"] == [win._script("time")]
    assert payload["message"] == payload["reason"] == finding.finding
    assert payload["message"] and payload["status"] == "warning"
    assert payload["evidence"][0]["stdout"] == stdout.decode()
    assert payload["template_threshold_eligible"] is False


def test_empty_success_retains_zero_rows_without_invented_placeholder(connect):
    connect([Channel(envelope([]))])
    payload = win.collect_windows_host_findings(None, [item("crontab")])[0].raw_data
    assert payload["rows"] == [] and payload["row_count"] == 0
    assert payload["state"] == "collected" and payload["status"] == "info"
    assert payload["truncated"] is False and payload["template_threshold_eligible"] is False


@pytest.mark.parametrize("section,query", [
    ("cpuasge", "cpu"), ("memusage", "os"), ("swapusage", "pagefile"), ("diskusage", "disk"),
])
def test_threshold_eligibility_requires_valid_measured_collection(connect, observations, section, query):
    connect([Channel(envelope(observations[query]))])
    payload = win.collect_windows_host_findings(None, [item(section)])[0].raw_data
    assert payload["state"] == "collected" and payload["template_threshold_eligible"] is True
    connect([Channel(envelope([{}]))])
    payload = win.collect_windows_host_findings(None, [item(section)])[0].raw_data
    assert payload["state"] == "collection_failed" and payload["template_threshold_eligible"] is False
    assert payload["rows"] == [{"query": query}] and payload["row_count"] == 1
    assert payload["status"] == "warning" and "采集数据无效" in payload["message"]


@pytest.mark.parametrize("stdout,limit", [
    (b"x" * 101, 100),
    (envelope([], "collection_failed", "Row limit exceeded"), 65536),
    (envelope(["now"] * (win.MAX_ROWS + 1)), 65536),
])
def test_evidence_reports_truncation_without_guessing_total_rows(connect, monkeypatch, stdout, limit):
    monkeypatch.setattr(win, "MAX_OUTPUT_BYTES", limit)
    connect([Channel(stdout)])
    payload = win.collect_windows_host_findings(None, [item("time")])[0].raw_data
    assert payload["truncated"] is True
    assert payload["rows"] == [] and payload["row_count"] == 0
    assert payload["state"] == "collection_failed" and payload["status"] == "warning"
    assert payload["message"] and payload["template_threshold_eligible"] is False
