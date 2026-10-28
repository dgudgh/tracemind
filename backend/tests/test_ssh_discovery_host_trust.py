"""Host-key trust boundary for automatically discovered SSH peers."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import ssh_executor
from app.services.ssh_executor import ManagedSSHClient, SSHConnectionError


class FakeAuthenticationException(Exception):
    pass


class FakeSSHException(Exception):
    pass


class FakeBadHostKeyException(FakeSSHException):
    pass


class FakeRejectPolicy:
    pass


class FakeAutoAddPolicy:
    pass


class FakeSSHClient:
    def __init__(self, *, outcome="ok", load_error=None):
        self.outcome = outcome
        self.load_error = load_error
        self.operations = []
        self.policy = None
        self.connect_kwargs = None

    def load_system_host_keys(self):
        self.operations.append(("load_system_host_keys",))
        if self.load_error:
            raise self.load_error

    def load_host_keys(self, filename):
        self.operations.append(("load_host_keys", filename))

    def set_missing_host_key_policy(self, policy):
        self.policy = policy
        self.operations.append(("set_policy", type(policy)))

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.operations.append(("connect", kwargs["hostname"], kwargs["port"]))
        if self.outcome == "mismatch":
            raise FakeBadHostKeyException("raw mismatched key material")
        if self.outcome == "missing":
            raise FakeSSHException("raw host was not found in known_hosts")
        return True

    def close(self):
        self.operations.append(("close",))


def _paramiko(client):
    return SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=FakeRejectPolicy,
        AutoAddPolicy=FakeAutoAddPolicy,
        AuthenticationException=FakeAuthenticationException,
        BadHostKeyException=FakeBadHostKeyException,
        SSHException=FakeSSHException,
    )


def _known_hosts(monkeypatch, tmp_path: Path, *, exists=True):
    home = tmp_path / "home"
    known_hosts = home / ".ssh" / "known_hosts"
    if exists:
        known_hosts.parent.mkdir(parents=True)
        known_hosts.write_text("synthetic-host-key\n", encoding="utf-8")
    monkeypatch.setattr(ssh_executor.Path, "home", classmethod(lambda cls: home))
    return known_hosts


def test_known_host_mode_loads_trust_before_connect(monkeypatch, tmp_path):
    known_hosts = _known_hosts(monkeypatch, tmp_path)
    transport = FakeSSHClient()
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    client = ManagedSSHClient(
        "db.example", username="ops", password="password-not-logged",
        require_known_host=True,
    )
    assert client.connect() is True

    assert isinstance(transport.policy, FakeRejectPolicy)
    assert transport.operations[:4] == [
        ("load_system_host_keys",),
        ("load_host_keys", str(known_hosts)),
        ("set_policy", FakeRejectPolicy),
        ("connect", "db.example", 22),
    ]
    assert transport.connect_kwargs["allow_agent"] is False
    assert transport.connect_kwargs["look_for_keys"] is False


def test_mismatched_host_key_is_rejected_without_raw_key_details(monkeypatch, tmp_path):
    _known_hosts(monkeypatch, tmp_path)
    transport = FakeSSHClient(outcome="mismatch")
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    with pytest.raises(SSHConnectionError) as raised:
        ManagedSSHClient(
            "db.example", username="ops", password="password-not-logged",
            require_known_host=True,
        ).connect()

    assert str(raised.value) == "SSH主机密钥不匹配，已拒绝连接"
    assert "raw mismatched" not in str(raised.value)
    assert not any(operation[0] == "set_policy" and operation[1] is FakeAutoAddPolicy
                   for operation in transport.operations)


def test_missing_host_key_is_rejected_by_policy(monkeypatch, tmp_path):
    _known_hosts(monkeypatch, tmp_path, exists=False)
    transport = FakeSSHClient(outcome="missing")
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    with pytest.raises(SSHConnectionError) as raised:
        ManagedSSHClient(
            "new-peer.example", username="ops", password="password-not-logged",
            require_known_host=True,
        ).connect()

    assert isinstance(transport.policy, FakeRejectPolicy)
    assert not any(operation[0] == "load_host_keys" for operation in transport.operations)
    assert str(raised.value) == "SSH主机密钥未受信任或握手校验失败，已拒绝连接"
    assert "password-not-logged" not in str(raised.value)


def test_known_hosts_load_failure_is_fail_closed(monkeypatch, tmp_path):
    _known_hosts(monkeypatch, tmp_path)
    transport = FakeSSHClient(load_error=OSError("synthetic unreadable trust store"))
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    with pytest.raises(SSHConnectionError) as raised:
        ManagedSSHClient("db.example", require_known_host=True).connect()

    assert str(raised.value) == "SSH主机密钥信任库加载失败，已拒绝连接"
    assert not any(operation[0] == "connect" for operation in transport.operations)
    assert not any(operation[0] == "set_policy" and operation[1] is FakeAutoAddPolicy
                   for operation in transport.operations)


def test_non_default_port_is_bound_into_strict_connect(monkeypatch, tmp_path):
    _known_hosts(monkeypatch, tmp_path)
    transport = FakeSSHClient()
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    ManagedSSHClient("db.example", port=2222, require_known_host=True).connect()

    assert isinstance(transport.policy, FakeRejectPolicy)
    assert transport.connect_kwargs["hostname"] == "db.example"
    assert transport.connect_kwargs["port"] == 2222
    assert ("connect", "db.example", 2222) in transport.operations


def test_default_mode_keeps_existing_auto_add_behavior(monkeypatch):
    transport = FakeSSHClient()
    monkeypatch.setattr(ssh_executor, "_get_ssh_paramiko", lambda: _paramiko(transport))

    ManagedSSHClient("configured.example").connect()

    assert isinstance(transport.policy, FakeAutoAddPolicy)
    assert not any(operation[0] in {"load_system_host_keys", "load_host_keys"}
                   for operation in transport.operations)
