from __future__ import annotations

import socket

import pytest

from app.runtime.engine_host.ipc import IpcError, decode_frame, encode_frame


def test_frame_roundtrip_and_replay_rejected():
    key = b"k" * 32
    frame = encode_frame({"method": "selftest", "params": {}}, key, 1)
    assert decode_frame(frame[4:], key, 1)["method"] == "selftest"
    with pytest.raises(IpcError) as error:
        decode_frame(frame[4:], key, 2)
    assert error.value.code == "frame_sequence_invalid"


def test_tampered_mac_and_oversized_prefix_rejected():
    key = b"k" * 32
    frame = bytearray(encode_frame({"a": 1}, key, 0)[4:])
    frame[0] ^= 1
    with pytest.raises(IpcError) as error:
        decode_frame(bytes(frame), key, 0)
    assert error.value.code == "frame_mac_invalid"


def test_unknown_frame_fields_rejected():
    key = b"k" * 32
    import hashlib
    import hmac
    import json
    body = json.dumps({"seq": 0, "payload": {}, "extra": 1}, separators=(",", ":")).encode()
    mac = hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    with pytest.raises(IpcError) as error:
        decode_frame(mac + b"." + body, key, 0)
    assert error.value.code == "frame_envelope_invalid"


def test_bool_sequence_and_deep_json_are_rejected():
    import hashlib
    import hmac
    import json
    key = b"k" * 32
    body = json.dumps({"seq": True, "payload": {}}, separators=(",", ":")).encode()
    mac = hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    with pytest.raises(IpcError) as error:
        decode_frame(mac + b"." + body, key, 1)
    assert error.value.code == "frame_sequence_invalid"
    deep = b'{"a":' * 40 + b"1" + b"}" * 40
    body = b'{"seq":0,"payload":' + deep + b"}"
    mac = hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    with pytest.raises(IpcError) as error:
        decode_frame(mac + b"." + body, key, 0)
    assert error.value.code == "frame_json_too_deep"


def test_provider_and_host_use_the_same_depth_rule():
    import importlib.util
    from pathlib import Path
    path = Path("backend/tests/fixtures/engine_providers/fake_provider.py")
    spec = importlib.util.spec_from_file_location("fixture_provider_depth", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deep = b'{"a":' * 40 + b"1" + b"}" * 40
    with pytest.raises(ValueError):
        module.strict_loads(deep)
    with pytest.raises(ValueError):
        module.strict_loads(b'{"a":1,"a":2}')
    with pytest.raises(ValueError):
        module.strict_loads(b'{"a":NaN}')
