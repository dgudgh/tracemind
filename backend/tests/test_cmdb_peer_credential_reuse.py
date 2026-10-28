"""Synthetic discovery checks; never connects to a database or host outside SQLite."""
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cmdb import service
from app.models.connection_certificate import ConnectionCertificateProfile
from app.models.db_instance import DBInstance
from app.services import host_overview


@pytest.fixture
def seed():
    engine = create_engine("sqlite:///:memory:")
    ConnectionCertificateProfile.__table__.create(engine)
    DBInstance.__table__.create(engine)
    with sessionmaker(bind=engine)() as db:
        profile = ConnectionCertificateProfile(name="cluster-tls", profile_type="pem")
        source = DBInstance(instance_name="primary", db_type="mysql", host="198.51.100.1",
                            port=3307, username="monitor", password_cipher="env://DB_SECRET",
                            sslmode="verify-full", tls_server_name="cluster.example", tls_verify=1,
                            certificate_profile=profile, enabled=1,
                            ssh_host="198.51.100.1", ssh_port=2222, ssh_username="ops",
                            ssh_password_cipher="env://SSH_SECRET")
        db.add(source)
        db.commit()
        yield db, source
    engine.dispose()


def seed_profile():
    return {"architecture": {
        "architecture_type": "replication", "role_type": "primary",
        "server_uuid": "primary-uuid", "server_id": 1,
        "members": [
            {"server_uuid": "uuid-b", "server_id": 3, "host": "", "port": 3307},
            {"server_uuid": "uuid-a", "server_id": 2, "host": "", "port": 3307},
        ],
        "peer_endpoint_candidates": [
            {"host": "198.51.100.2", "port": 3307},
            {"host": "198.51.100.3", "port": 3307},
        ],
    }}


def test_candidate_address_binds_by_uuid_and_reuses_database_and_tls(seed, monkeypatch):
    db, source = seed
    observed = []

    def connect(instance):
        if instance.id == source.id:
            return seed_profile()
        observed.append(instance)
        name, server_id = {"198.51.100.2": ("uuid-a", 2), "198.51.100.3": ("uuid-b", 3)}[instance.host]
        return {"version": "8.0.44", "architecture": {
            "server_uuid": name, "server_id": server_id, "role_type": "replica",
            "hostname": name, "replication_delay_seconds": 0,
        }}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, warnings, count = service._probe_internal_instances(db)
    assert count == 1 and not warnings
    members = {item["server_uuid"]: item for item in profiles[source.id]["members"]}
    assert members["uuid-a"]["host"] == "198.51.100.2"
    assert members["uuid-b"]["host"] == "198.51.100.3"
    assert all(item["db_probe_status"] == "verified" for item in members.values())
    for peer in observed:
        assert (peer.username, peer.password_cipher) == (source.username, source.password_cipher)
        assert (peer.certificate_profile_id, peer.tls_verify, peer.tls_server_name, peer.sslmode) == (
            source.certificate_profile_id, 1, "cluster.example", "verify-full")
        assert peer.certificate_profile is source.certificate_profile
    assert db.query(DBInstance).count() == 1
    assert "DB_SECRET" not in str(profiles)


@pytest.mark.parametrize("failure", ["mismatched_uuid", "authentication_failed"])
def test_failed_candidates_do_not_invent_member_addresses(seed, monkeypatch, failure):
    db, source = seed

    def connect(instance):
        if instance.id == source.id:
            return seed_profile()
        if failure == "authentication_failed":
            raise RuntimeError("authentication failed")
        return {"architecture": {"server_uuid": "unrelated-server", "server_id": 2}}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, warnings, _ = service._probe_internal_instances(db)
    assert len(warnings) == 2
    assert all(not item.get("host") and not item.get("direct_probe")
               for item in profiles[source.id]["members"])
    assert source.password_cipher == "env://DB_SECRET"


def test_candidates_do_not_replace_an_already_resolved_member(seed, monkeypatch):
    db, source = seed

    def connect(instance):
        if instance.id == source.id:
            profile = seed_profile()
            profile["architecture"]["members"][0]["host"] = "198.51.100.50"
            return profile
        uuid = "uuid-a" if instance.host == "198.51.100.2" else "uuid-b"
        return {"architecture": {"server_uuid": uuid, "role_type": "replica"}}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    profiles, _, _ = service._probe_internal_instances(db)
    members = {item["server_uuid"]: item for item in profiles[source.id]["members"]}
    assert members["uuid-b"]["host"] == "198.51.100.50"
    assert members["uuid-a"]["host"] == "198.51.100.2"


def test_ssh_reuse_is_limited_to_database_verified_members(seed, monkeypatch):
    db, source = seed
    members = [
        {"host": "198.51.100.2", "direct_probe": True},
        {"host": "198.51.100.2", "direct_probe": True},
        {"host": "198.51.100.3", "direct_probe": False},
        {"host": "", "direct_probe": True},
    ]
    calls = []

    def ssh(instance, **kwargs):
        calls.append((instance, kwargs))
        return {"host_basic": {"hostname": "peer-host", "os_name": "Rocky Linux"}}

    monkeypatch.setattr(host_overview, "get_instance_host_overview", ssh)
    profiles, warnings = service._probe_internal_hosts(db, {
        source.id: {"discovery_status": "success", "members": members},
    })
    assert not warnings
    peer_calls = [(item, options) for item, options in calls if item.id is None]
    assert len(peer_calls) == 1
    peer = peer_calls[0][0]
    assert (peer.ssh_host, peer.ssh_port, peer.ssh_username, peer.ssh_password_cipher) == (
        "198.51.100.2", 2222, "ops", "env://SSH_SECRET")
    assert members[0]["ssh_probe_status"] == members[1]["ssh_probe_status"] == "verified"
    assert "ssh_probe_status" not in members[2]
    assert profiles["198.51.100.2"]["hostname"] == "peer-host"
    assert "SSH_SECRET" not in str(profiles) + str(members)
    assert db.query(DBInstance).count() == 1


def test_ssh_failure_preserves_database_verification(seed, monkeypatch):
    db, source = seed
    member = {"host": "198.51.100.2", "direct_probe": True, "db_probe_status": "verified"}
    monkeypatch.setattr(host_overview, "get_instance_host_overview", Mock(return_value={"host_basic": None}))
    _, warnings = service._probe_internal_hosts(db, {
        source.id: {"discovery_status": "success", "members": [member]},
    })
    assert member["ssh_probe_status"] == "failed"
    assert member["direct_probe"] is True and member["db_probe_status"] == "verified"
    assert any("198.51.100.2" in warning for warning in warnings)


def test_same_peer_host_is_tried_once_across_seed_ssh_ports(seed, monkeypatch):
    db, source = seed
    other = DBInstance(instance_name="other-seed", db_type="mysql", host="198.51.100.5",
                       port=3307, username="monitor", password_cipher="env://OTHER_DB",
                       ssh_host="198.51.100.5", ssh_port=2223, ssh_username="other-ops",
                       ssh_password_cipher="env://OTHER_SSH", enabled=1)
    db.add(other)
    db.commit()
    first, second = [{"host": "198.51.100.2", "direct_probe": True} for _ in range(2)]
    calls = []

    def ssh(instance, **kwargs):
        if instance.id is None:
            calls.append(instance)
        return {"host_basic": {"hostname": "peer-host"}}

    monkeypatch.setattr(host_overview, "get_instance_host_overview", ssh)
    service._probe_internal_hosts(db, {
        source.id: {"discovery_status": "success", "members": [first]},
        other.id: {"discovery_status": "success", "members": [second]},
    })
    assert len(calls) == 1
    assert calls[0].ssh_port == 2222
    assert first["ssh_probe_status"] == second["ssh_probe_status"] == "verified"


def test_ssh_discovery_commands_are_identity_only(seed, monkeypatch):
    _, source = seed
    command_sets = []

    def batch(instance, commands, **options):
        command_sets.append(commands)
        assert options == {"platform_probe": True, "require_known_host": True}
        return {"hostname": "peer", "os": 'NAME="Rocky Linux"\nVERSION_ID="9.6"',
                "cpu_count": "4", "memory": "Mem: 8192 1000 7192 0 0 0"}

    monkeypatch.setattr(host_overview, "_run_batch", batch)
    result = host_overview.get_instance_host_overview(source, discovery_only=True, require_known_host=True)
    assert result["host_basic"]["hostname"] == "peer"
    assert set(command_sets[0]) == {"hostname", "ip", "os", "cpu_count", "cpu_model", "memory"}


def test_verified_peer_probes_reach_current_ha_verification_count(seed, monkeypatch):
    from app.core.database import Base
    from app.cmdb import ha_diagnostics, normalized_service

    db, source = seed
    Base.metadata.create_all(db.get_bind())

    def connect(instance):
        if instance.id == source.id:
            return seed_profile()
        uuid, server_id = {"198.51.100.2": ("uuid-a", 2), "198.51.100.3": ("uuid-b", 3)}[instance.host]
        return {"architecture": {"server_uuid": uuid, "server_id": server_id,
                                 "hostname": uuid, "role_type": "replica"}}

    monkeypatch.setattr(service.instance_service, "test_connection", connect)
    monkeypatch.setattr(host_overview, "get_instance_host_overview", Mock(return_value={
        "host_basic": {"hostname": "synthetic-host", "os_name": "Linux"},
    }))
    service.apply_internal_discovery(db, probe_live=True)
    cluster = normalized_service.list_clusters(db)[0]
    assessment = ha_diagnostics.assess_cluster(db, cluster["id"])
    assert assessment["summary"]["verified_count"] == 3
    assert assessment["summary"]["reachable_count"] == 3
    assets = service.list_inventory(db, asset_type="database_instance")
    assert len(assets) == 3
    assert "DB_SECRET" not in str(assets) and "SSH_SECRET" not in str(assets)
