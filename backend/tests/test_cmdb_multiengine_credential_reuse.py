"""Cross-engine discovery orchestration with synthetic connections only."""
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import service
from app.cmdb.peer_connection import verify_peer_identity
from app.models.connection_certificate import ConnectionCertificateProfile
from app.models.db_instance import DBInstance
from app.services import host_overview


CASES = {
    "oracle_dg": ("oracle", 1521, "data_guard", {
        "db_unique_name": "standby", "oracle_connect_type": "service_name",
        "service_name": "standby_service", "endpoint_unresolved": False,
    }, {"db_unique_name": "standby", "instance_name": "standby1"}),
    "oracle_rac": ("oracle", 1521, "rac", {
        "db_unique_name": "main", "instance_name": "main2",
    }, {"db_unique_name": "main", "instance_name": "main2"}),
    "postgresql": ("postgresql", 5544, "streaming_replication", {
        "port_inferred": True,
    }, {"system_identifier": "cluster-1", "in_recovery": True,
        "upstream_host": "198.51.100.1", "upstream_port": 5544}),
    "sqlserver": ("sqlserver", 1444, "always_on", {
        "replica_id": "replica-2", "availability_group_id": "ag-1",
        "server_name": "SQL02\\REPORTING", "instance_name": "REPORTING",
        "port_inferred": True,
    }, {"replica_id": "replica-2", "availability_group_id": "ag-1",
        "server_name": "SQL02\\REPORTING", "instance_name": "REPORTING"}),
}


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    ConnectionCertificateProfile.__table__.create(engine)
    DBInstance.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def arrange(db, case):
    engine, port, architecture, extra, actual = CASES[case]
    seed = DBInstance(instance_name="seed", db_type=engine, host="198.51.100.1", port=port,
                      username="monitor", password_cipher="env://DATABASE_PASSWORD", enabled=1,
                      oracle_connect_type="sid", sid="main1", database_name="control",
                      sslmode="verify-full", encrypt="require", tls_verify=1,
                      ssh_host="198.51.100.1", ssh_username="ops", ssh_port=2222,
                      ssh_password_cipher="env://SSH_PASSWORD")
    db.add(seed)
    db.commit()
    member = {"host": "198.51.100.2", "port": port, "role_type": "replica", **extra}
    profile = {"architecture_type": architecture, "role_type": "primary", "members": [member],
               "system_identifier": "cluster-1" if engine == "postgresql" else None,
               "availability_group_id": "ag-1" if engine == "sqlserver" else None}
    return seed, profile, dict(actual)


@pytest.mark.parametrize("case", CASES)
def test_database_and_ssh_credentials_are_reused_for_each_engine(db, monkeypatch, case):
    seed, profile, actual = arrange(db, case)
    peers = []

    def connect(item):
        if item.id == seed.id:
            return {"architecture": profile}
        peers.append(item)
        return {"version": "synthetic-version", "architecture": {
            "architecture_type": profile["architecture_type"], "role_type": "replica", **actual,
        }}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, warnings, _ = service._probe_internal_instances(db)
    assert not warnings and len(peers) == 1
    peer = peers[0]
    assert (peer.username, peer.password_cipher) == (seed.username, seed.password_cipher)
    assert (peer.sslmode, peer.encrypt, peer.tls_verify) == (seed.sslmode, seed.encrypt, seed.tls_verify)
    if case == "oracle_dg":
        assert (peer.oracle_connect_type, peer.service_name, peer.sid) == ("service_name", "standby_service", None)
    if case == "oracle_rac":
        assert (peer.oracle_connect_type, peer.sid, peer.service_name) == ("sid", "main2", None)
    calls = []

    def ssh(item, **options):
        if item.id is None:
            calls.append(item)
            assert options["require_known_host"] is True
        assert options["discovery_only"] is True
        return {"host_basic": {"hostname": "peer-host", "os_name": "Windows Server" if case == "sqlserver" else "Linux"}}

    monkeypatch.setattr(host_overview, "get_instance_host_overview", ssh)
    hosts, warnings = service._probe_internal_hosts(db, profiles)
    assert not warnings and len(calls) == 1
    assert (calls[0].ssh_host, calls[0].ssh_username, calls[0].ssh_password_cipher, calls[0].ssh_port) == (
        "198.51.100.2", "ops", seed.ssh_password_cipher, 2222)
    member = profiles[seed.id]["members"][0]
    assert member["db_probe_status"] == member["ssh_probe_status"] == "verified"
    assert "DATABASE_PASSWORD" not in str(profiles) and "SSH_PASSWORD" not in str(hosts)
    assert db.query(DBInstance).count() == 1


@pytest.mark.parametrize("case", CASES)
def test_wrong_peer_identity_is_not_confirmed_or_sent_ssh_credentials(db, monkeypatch, case):
    seed, profile, actual = arrange(db, case)
    field = {"oracle_dg": "db_unique_name", "oracle_rac": "instance_name",
             "postgresql": "system_identifier", "sqlserver": "replica_id"}[case]
    actual[field] = "wrong-target"
    monkeypatch.setattr(service.instance_service, "test_connection", lambda item: {
        "architecture": profile if item.id == seed.id else actual,
    })
    profiles, warnings, _ = service._probe_internal_instances(db)
    assert warnings
    member = profiles[seed.id]["members"][0]
    assert member["direct_probe"] is False
    ssh = Mock(return_value={"host_basic": {"hostname": "seed"}})
    monkeypatch.setattr(host_overview, "get_instance_host_overview", ssh)
    service._probe_internal_hosts(db, profiles)
    assert all(call.args[0].id == seed.id for call in ssh.call_args_list)


def test_unresolved_oracle_alias_is_not_sent_to_tcp_or_ssh(db, monkeypatch):
    seed, profile, _ = arrange(db, "oracle_dg")
    profile["members"] = [{"host": "", "reported_host": "standby_alias",
                           "connect_identifier": "standby_alias", "endpoint_unresolved": True}]
    connect = Mock(return_value={"architecture": profile})
    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, _, _ = service._probe_internal_instances(db)
    assert connect.call_count == 1
    assert profiles[seed.id]["members"][0]["db_probe_status"] == "needs_endpoint"
    ssh = Mock(return_value={"host_basic": {"hostname": "seed"}})
    monkeypatch.setattr(host_overview, "get_instance_host_overview", ssh)
    service._probe_internal_hosts(db, profiles)
    assert ssh.call_count == 1 and ssh.call_args.args[0].id == seed.id


def test_windows_ssh_identity_uses_one_login_and_read_only_commands(monkeypatch):
    client = Mock()
    client.execute_command.side_effect = [
        {"exit_code": 1, "stdout": ""},
        {"exit_code": 0, "stdout": "SQL02"},
        {"exit_code": 0, "stdout": json.dumps({"os_name": "Microsoft Windows Server 2022",
         "os_version": "10.0.20348", "cpu_count": 8, "memory_total_mb": 16384})},
    ]
    factory = Mock(return_value=client)
    monkeypatch.setattr(host_overview, "ManagedSSHClient", factory)
    monkeypatch.setattr(host_overview, "decrypt_password", lambda _: "synthetic-password")
    peer = DBInstance(ssh_host="sql02", ssh_username="ops", ssh_password_cipher="unused")
    result = host_overview.get_instance_host_overview(peer, discovery_only=True, require_known_host=True)
    assert result["host_basic"]["hostname"] == "SQL02"
    assert result["host_basic"]["os_name"] == "Microsoft Windows Server 2022"
    client.connect.assert_called_once()
    assert factory.call_args.kwargs["require_known_host"] is True
    client.close.assert_called_once()
    assert "Get-CimInstance" in client.execute_command.call_args_list[2].args[0]


@pytest.mark.parametrize("upstream", [{}, {"upstream_host": "198.51.100.99", "upstream_port": 5544},
                                     {"upstream_host": "198.51.100.1", "upstream_port": 5432}])
def test_pg_without_cluster_identifier_requires_complete_matching_receiver(upstream):
    with pytest.raises(ValueError):
        verify_peer_identity("postgresql", {"role_type": "replica"}, {"in_recovery": True, **upstream},
                             {"connection_host": "198.51.100.1", "connection_port": 5544})
    verify_peer_identity("postgresql", {"role_type": "replica"},
                         {"in_recovery": True, "upstream_host": "198.51.100.1", "upstream_port": 5544},
                         {"connection_host": "198.51.100.1", "connection_port": 5544})


def test_configured_peer_on_another_pg_port_does_not_capture_discovered_member(db, monkeypatch):
    seed, profile, actual = arrange(db, "postgresql")
    profile["members"][0].update(port=5434, name="same-label")
    other = DBInstance(instance_name="same-label", database_name="same-label", db_type="postgresql",
                       host="198.51.100.2", port=5432, username="different", password_cipher="unused", enabled=1)
    db.add(other)
    db.commit()
    calls = []

    def connect(item):
        calls.append(item)
        if item.id == seed.id:
            return {"architecture": profile}
        if item.id == other.id:
            return {"architecture": {"system_identifier": "other-cluster", "members": []}}
        return {"architecture": actual}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, _, _ = service._probe_internal_instances(db)
    member = profiles[seed.id]["members"][0]
    assert member["port"] == 5434 and member["db_probe_status"] == "verified"
    assert member.get("managed_instance_id") != other.id
    assert any(item.id is None and item.port == 5434 and item.username == seed.username for item in calls)


def test_configured_sql_peer_must_also_match_replica_and_group(db, monkeypatch):
    seed, profile, _ = arrange(db, "sqlserver")
    other = DBInstance(instance_name="SQL02", db_type="sqlserver", host="198.51.100.2",
                       port=1444, username="own-user", password_cipher="own-secret", enabled=1)
    db.add(other)
    db.commit()
    monkeypatch.setattr(service.instance_service, "test_connection", lambda item: {
        "architecture": profile if item.id == seed.id else {
            "replica_id": "other-replica", "availability_group_id": "other-ag", "members": []},
    })
    profiles, warnings, _ = service._probe_internal_instances(db)
    assert warnings
    assert profiles[seed.id]["members"][0]["direct_probe"] is False


def test_successful_authentication_does_not_erase_replication_warning(db, monkeypatch):
    seed, profile, actual = arrange(db, "postgresql")
    profile["members"][0]["health_status"] = "warning"
    monkeypatch.setattr(service.instance_service, "test_connection", lambda item: {
        "architecture": profile if item.id == seed.id else actual,
    })
    profiles, warnings, _ = service._probe_internal_instances(db)
    member = profiles[seed.id]["members"][0]
    assert not warnings and member["db_probe_status"] == "verified"
    assert member["health_status"] == "warning"
