"""Synthetic tests for bounded Oracle Net alias discovery."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.cmdb import oracle_network


def _source(**updates):
    values = {
        "db_type": "oracle",
        "instance_name": "mesdb",
        "database_name": "MESDB",
        "sid": "mesdb",
        "ssh_host": "192.0.2.10",
        "ssh_port": 22,
        "ssh_username": "ops",
        "ssh_password_cipher": "secret-reference",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _result(stdout="", *, ok=True):
    return {"ok": ok, "success": ok, "exit_code": 0 if ok else 1, "stdout": stdout}


def _session(monkeypatch, side_effect):
    client = Mock()
    client.execute_command.side_effect = side_effect
    state = {"entered": 0, "exited": 0}

    @contextmanager
    def connection(source, timeout):
        state["entered"] += 1
        assert source.ssh_host == "192.0.2.10"
        assert timeout == oracle_network.SSH_CONNECT_TIMEOUT_SECONDS
        try:
            yield client
        finally:
            state["exited"] += 1

    monkeypatch.setattr(oracle_network, "ssh_connection", connection)
    return client, state


@pytest.mark.parametrize(
    "source",
    [
        _source(db_type="mysql"),
        _source(ssh_host=""),
        _source(ssh_username=""),
        _source(ssh_password_cipher=""),
    ],
)
def test_only_configured_oracle_ssh_sources_are_attempted(monkeypatch, source):
    connection = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(oracle_network, "ssh_connection", connection)

    assert oracle_network.resolve_oracle_aliases(source, ["reportdb"]) == {}
    connection.assert_not_called()


def test_single_home_resolves_descriptor_without_returning_raw_output(monkeypatch):
    descriptor = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.21)(PORT=1521))"
        "(CONNECT_DATA=(SERVICE_NAME=reportdb)))"
    )
    client, state = _session(monkeypatch, [
        _result("ENV|/u01/app/oracle/product/19.3.0/db|\n"),
        _result(f"TNS Ping Utility\nAttempting to contact {descriptor}\nOK (20 msec)"),
    ])

    result = oracle_network.resolve_oracle_aliases(_source(), ["reportdb"])

    assert result == {"reportdb": {
        "host": "192.0.2.21",
        "port": 1521,
        "oracle_connect_type": "service_name",
        "service_name": "reportdb",
        "sid": "",
        "connect_identifier": "reportdb",
        "endpoint_unresolved": False,
        "endpoint_candidates": [],
        "resolution_source": "oracle_tnsping",
    }}
    assert state == {"entered": 1, "exited": 1}
    assert client.execute_command.call_count == 2
    command = client.execute_command.call_args_list[1].args[0]
    assert "ORACLE_HOME=/u01/app/oracle/product/19.3.0/db" in command
    assert "/u01/app/oracle/product/19.3.0/db/bin/tnsping reportdb" in command
    assert "Attempting to contact" not in str(result)
    assert all(call.kwargs["timeout"] <= 8 for call in client.execute_command.call_args_list)


def test_pmon_home_is_preferred_to_oratab_and_paths_are_shell_quoted(monkeypatch):
    discovery = "\n".join([
        "ENV||",
        "PMON|ora_pmon_mesdb|/u01/oracle db|/u01/tns admin",
        "ORATAB|other|/opt/oracle/other",
        "ORATAB|mesdb|/u01/oracle db",
    ])
    descriptor = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=standby.example)(PORT=1522))"
        "(CONNECT_DATA=(SID=REPORT1)))"
    )
    client, _ = _session(monkeypatch, [
        _result(discovery),
        _result(f"Attempting to contact {descriptor}\nOK (8 msec)"),
        _result("TNS-03505: Failed to resolve name", ok=False),
    ])

    result = oracle_network.resolve_oracle_aliases(_source(), ["REPORT1"])

    assert result["REPORT1"]["sid"] == "REPORT1"
    first_probe = client.execute_command.call_args_list[1].args[0]
    assert "ORACLE_HOME='/u01/oracle db'" in first_probe
    assert "TNS_ADMIN='/u01/tns admin'" in first_probe
    assert len(client.execute_command.call_args_list) == 3


def test_different_homes_return_unresolved_candidates_instead_of_selecting_first(monkeypatch):
    first = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.31)(PORT=1521))"
        "(CONNECT_DATA=(SERVICE_NAME=reportdb)))"
    )
    second = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.32)(PORT=1521))"
        "(CONNECT_DATA=(SERVICE_NAME=reportdb)))"
    )
    _session(monkeypatch, [
        _result("ENV|/oracle/one|\nPMON|ora_pmon_mesdb|/oracle/two|"),
        _result(f"Attempting to contact {first}\nOK (1 msec)"),
        _result(f"Attempting to contact {second}\nOK (1 msec)"),
    ])

    metadata = oracle_network.resolve_oracle_aliases(_source(), ["reportdb"])["reportdb"]

    assert metadata["host"] == "" and metadata["port"] is None
    assert metadata["endpoint_unresolved"] is True
    assert metadata["service_name"] == "reportdb"
    assert metadata["endpoint_candidates"] == [
        {"host": "192.0.2.31", "port": 1521},
        {"host": "192.0.2.32", "port": 1521},
    ]


def test_multi_address_descriptor_keeps_candidates_and_never_chooses_first(monkeypatch):
    descriptor = (
        "(DESCRIPTION=(ADDRESS_LIST="
        "(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.41)(PORT=1521))"
        "(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.42)(PORT=1521)))"
        "(CONNECT_DATA=(SERVICE_NAME=reportdb)))"
    )
    _session(monkeypatch, [
        _result("PMON|ora_pmon_mesdb|/oracle/db|"),
        _result(f"Attempting to contact {descriptor}\nOK (2 msec)"),
    ])

    metadata = oracle_network.resolve_oracle_aliases(_source(), ["reportdb"])["reportdb"]

    assert metadata["host"] == "" and metadata["endpoint_unresolved"] is True
    assert metadata["endpoint_candidates"] == [
        {"host": "192.0.2.41", "port": 1521},
        {"host": "192.0.2.42", "port": 1521},
    ]


def test_alias_and_home_limits_are_enforced(monkeypatch):
    homes = "\n".join(f"ORATAB|db{index}|/oracle/db{index}" for index in range(1, 7))
    client, _ = _session(monkeypatch, [_result(homes)] + [_result("not resolved", ok=False)] * 32)
    aliases = ["bad alias", "x;id", *[f"DB{index}" for index in range(1, 11)]]

    assert oracle_network.resolve_oracle_aliases(_source(), aliases) == {}
    assert client.execute_command.call_count == 1 + (
        oracle_network.MAX_ALIASES * oracle_network.MAX_ORACLE_HOMES
    )
    commands = [call.args[0] for call in client.execute_command.call_args_list[1:]]
    assert all("bad alias" not in command and "x;id" not in command for command in commands)
    assert not any("/oracle/db5" in command or "/oracle/db6" in command for command in commands)


@pytest.mark.parametrize(
    "probe",
    [
        _result("Attempting to contact (DESCRIPTION=(ADDRESS=(HOST=192.0.2.9)(PORT=1521)))"),
        _result("OK (1 msec)"),
        _result("Attempting to contact (DESCRIPTION=(ADDRESS=(HOST=192.0.2.9)(PORT=1521))\nOK (1 msec)"),
        _result("TNS-03505", ok=False),
    ],
)
def test_failed_or_incomplete_tnsping_is_omitted(monkeypatch, probe):
    _session(monkeypatch, [_result("ORATAB|mesdb|/oracle/db"), probe])
    assert oracle_network.resolve_oracle_aliases(_source(), ["reportdb"]) == {}


def test_ssh_failure_is_safely_suppressed(monkeypatch):
    @contextmanager
    def failed_connection(*_args, **_kwargs):
        raise RuntimeError("synthetic secret-bearing remote failure")
        yield  # pragma: no cover

    monkeypatch.setattr(oracle_network, "ssh_connection", failed_connection)

    assert oracle_network.resolve_oracle_aliases(_source(), ["reportdb"]) == {}
