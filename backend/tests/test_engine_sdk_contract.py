from __future__ import annotations

import pytest

from app.runtime.engine_sdk.contracts import (
    Descriptor, SdkError, canonical_json, negotiate, standard_result, validate_envelope,
)


def test_version_negotiation_accepts_s_and_s_minus_one():
    assert negotiate(["1.0", "0.9"]) == "1.0"
    assert negotiate(["0.9"]) == "0.9"
    with pytest.raises(SdkError) as error:
        negotiate(["2.0"])
    assert error.value.code == "sdk_version_unsupported"


def test_envelope_is_closed_and_method_is_fixed():
    ok = {"schema": "engine.rpc/v1", "request_id": "r1", "method": "invoke_read", "params": {}}
    assert validate_envelope(ok) is ok
    with pytest.raises(SdkError):
        validate_envelope({**ok, "extra": 1})
    with pytest.raises(SdkError):
        validate_envelope({**ok, "method": "shell"})


def test_descriptor_cannot_claim_a_real_engine():
    with pytest.raises(SdkError) as error:
        Descriptor("mysql", "p", "1.0.0", "a", "b", "1.0", "linux", "x86_64", "cp312", 1)
    assert error.value.code == "descriptor_engine_not_fixture"


def test_canonical_json_and_result_envelope():
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    result = standard_result(provider_version="1.0.0", values={"rows": []})
    assert result["schema"] == "engine.result/v1"
