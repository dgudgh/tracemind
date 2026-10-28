"""Portable signed-SDK client for Host-owned secret leases.

Construct only from the protected launch FD config; never log or persist broker_key.
"""
from __future__ import annotations

import os
import socket
import time
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import wire


class BrokerClientError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def broker_context(context):
    """Derive broker purpose from the standard SDK capability without altering it."""
    if not isinstance(context, dict) or type(context.get("capability_id")) is not str or not context["capability_id"]:
        raise BrokerClientError("broker_context_invalid")
    result = dict(context)
    if "purpose" in result and result["purpose"] != result["capability_id"]:
        raise BrokerClientError("broker_purpose_mismatch")
    result["purpose"] = result["capability_id"]
    return result


class BrokerClient:
    def __init__(self, broker_path, broker_pid, broker_key, identity):
        if (type(broker_path) is not str or not os.path.isabs(broker_path)
                or type(broker_pid) is not int or broker_pid <= 0
                or type(broker_key) is not str or len(broker_key) != 64
                or not isinstance(identity, dict)):
            raise BrokerClientError("broker_config_invalid")
        try:
            key = bytes.fromhex(broker_key)
        except ValueError as exc:
            raise BrokerClientError("broker_config_invalid") from exc
        for field in ("engine_id", "package_digest", "runtime_digest", "launch_instance_id"):
            if type(identity.get(field)) is not str or not identity[field]:
                raise BrokerClientError("broker_config_invalid")
        self.path = broker_path
        self.host_pid = broker_pid
        self.key = key
        self.identity = {field: identity[field] for field in
                         ("engine_id", "package_digest", "runtime_digest", "launch_instance_id")}

    @classmethod
    def from_launch_config(cls, config):
        broker = config["secret_broker"]
        return cls(broker["broker_path"], broker["broker_pid"], broker["broker_key"], config["identity"])

    def _call(self, method, lease_id, context):
        request_context = broker_context(context)
        if any(request_context.get(k) != v for k, v in self.identity.items()):
            raise BrokerClientError("broker_identity_mismatch")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)
            sock.connect(self.path)
            peer = wire.peer_credentials(sock)
            if peer.pid != self.host_pid or peer.uid != os.getuid():
                raise BrokerClientError("broker_host_identity_mismatch")
            client_nonce = os.urandom(32)
            wire.send_plain(sock, {"type": "hello", "launch_instance_id": self.identity["launch_instance_id"],
                                   "nonce": client_nonce.hex()})
            challenge = wire.recv_plain(sock)
            if set(challenge) != {"transcript", "proof"}:
                raise BrokerClientError("broker_handshake_invalid")
            transcript = challenge["transcript"]
            expected = {"protocol": "tmengine.secret-broker/v1", "connection_id": transcript.get("connection_id"),
                        "client_nonce": client_nonce.hex(), "server_nonce": transcript.get("server_nonce"),
                        "host_pid": peer.pid, "host_uid": peer.uid,
                        "provider_pid": os.getpid(), "provider_uid": os.getuid(),
                        "identity": self.identity, "deadline": transcript.get("deadline")}
            if transcript != expected or type(transcript["deadline"]) not in (int, float) or transcript["deadline"] <= time.time():
                raise BrokerClientError("broker_handshake_binding_invalid")
            server_nonce = bytes.fromhex(transcript["server_nonce"])
            if len(server_nonce) != 32:
                raise BrokerClientError("broker_nonce_invalid")
            wire.verify_mac(self.key, b"broker-host-to-provider\0" + wire.canonical(transcript),
                            bytes.fromhex(challenge["proof"]))
            wire.send_plain(sock, {"proof": wire.mac(self.key, b"broker-provider-to-host\0" + wire.canonical(transcript)).hex()})
            host_key, provider_key = tuple(HKDF(algorithm=hashes.SHA256(), length=32,
                salt=server_nonce + client_nonce, info=b"tmengine.secret-broker/v1\0" + direction + b"\0" + wire.canonical(transcript)).derive(self.key)
                for direction in (b"host-to-provider", b"provider-to-host"))
            wire.send_frame(sock, {"method": method, "lease_id": lease_id, "context": request_context},
                            provider_key, 0, connection_id=transcript["connection_id"],
                            direction="broker-provider-to-host", max_bytes=65536)
            response = wire.recv_frame(sock, host_key, 0, connection_id=transcript["connection_id"],
                                       direction="broker-host-to-provider", max_bytes=65536)
            wire.send_frame(sock, {"ack": True}, provider_key, 1, connection_id=transcript["connection_id"],
                            direction="broker-provider-to-host", max_bytes=65536)
            if response.get("ok") is not True:
                raise BrokerClientError(response.get("error", "broker_failed"))
            return response["result"]
        except (OSError, KeyError, ValueError, wire.IpcError) as exc:
            if isinstance(exc, BrokerClientError):
                raise
            raise BrokerClientError("broker_transport_failed") from exc
        finally:
            sock.close()

    def redeem(self, lease_id, context):
        """Return a secret dict in memory; caller must close target session then release."""
        result = self._call("redeem", lease_id, context)
        if not isinstance(result, dict):
            raise BrokerClientError("broker_response_invalid")
        return result

    def release(self, lease_id, context):
        """Acknowledge target connection/pool closure in the caller's finally block."""
        return self._call("release", lease_id, context)["released"]
