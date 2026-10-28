"""Core worker/Provider identity, independent of credentials and business imports."""
from __future__ import annotations

import hashlib
import json
import re
from types import MappingProxyType

FIELDS = frozenset({"core_release", "engine_id", "package_digest", "runtime_digest",
                    "sdk_version", "task_schema", "result_schema"})
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ExecutionIdentityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_identity(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ExecutionIdentityError("execution_identity_invalid")
    for key, item in value.items():
        if not isinstance(item, str) or not 1 <= len(item) <= 200:
            raise ExecutionIdentityError("execution_identity_invalid")
        if key in {"package_digest", "runtime_digest"} and not DIGEST.fullmatch(item):
            raise ExecutionIdentityError("execution_identity_invalid")
    if value["sdk_version"] not in {"1.0", "0.9"}:
        raise ExecutionIdentityError("execution_sdk_unsupported")
    if value["task_schema"] != "engine.task/v1" or value["result_schema"] != "engine.result/v1":
        raise ExecutionIdentityError("execution_schema_unsupported")
    return MappingProxyType(dict(value))


def identity_digest(value):
    validated = dict(validate_identity(value))
    return hashlib.sha256(json.dumps(validated, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def worker_matches(snapshot, worker_identity):
    """Legacy workers only claim legacy plans; pinned workers never claim unbound plans."""
    frozen = snapshot.get("execution_identity") if isinstance(snapshot, dict) else None
    if frozen is None:
        return worker_identity is None
    if worker_identity is None:
        return False
    try:
        return dict(validate_identity(frozen)) == dict(validate_identity(worker_identity))
    except ExecutionIdentityError:
        return False
