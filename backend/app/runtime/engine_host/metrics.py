"""Capacity evidence helpers for the fixture-only engine Host.

Missing OS measurements retain ``value=None`` and a reason.  They are never
silently turned into zero.  Calibration and formal runs share this schema so a
frozen profile can be rejected before a Provider starts.
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    import psutil
except Exception:  # pragma: no cover - explicit missing source is supported
    psutil = None


PROFILE_SCHEMA = "engine.capacity-profile/v2"
REPORT_SCHEMA = "engine.capacity-report/v2"
RAW_SCHEMA = "engine.capacity-raw/v2"

REQUIRED_METRICS_BY_KIND = {
    "single": {
        "metrics.startup.cold_ms.p95", "metrics.startup.warm_ms.p95",
        "metrics.invoke_read.combined_regular_ms.p95",
        "metrics.invoke_read.cases.over_limit.rejected",
        "metrics.lifecycle.drain_ms.max", "metrics.lifecycle.stop_ms.max",
        "metrics.lifecycle.max_residual_count",
        "metrics.lifecycle.rss_trend_bytes_per_round.value",
        "metrics.resources.rss_bytes.max", "metrics.resources.process_count.max",
        "metrics.resources.fd_count.max", "metrics.resources.queue_depth.max",
        "metrics.resources.inflight.max", "metrics.resources.ipc_connections.max",
    },
    "dual": {
        "metrics.dual.duration_ms", "metrics.dual.new_invoke_ms.p95",
        "metrics.dual.old_drain_ms.max", "metrics.dual.resources.rss_bytes.max",
        "metrics.dual.resources.fd_count.max", "metrics.dual.resources.process_count.max",
        "metrics.dual.resources.queue_depth.max", "metrics.dual.resources.inflight.max",
        "metrics.dual.resources.ipc_connections.max",
        "metrics.dual.resources.rss_trend_bytes_per_sample.value",
        "metrics.dual.final_residual_processes", "metrics.dual.task_failure_count",
        "metrics.dual.task_rejection_count",
    },
}


class CapacityEvidenceError(ValueError):
    """Stable fail-closed capacity/profile validation error."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_directory(path: Path) -> str:
    root = Path(path).resolve()
    entries = []
    for item in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        entries.append({"path": item.relative_to(root).as_posix(), "size": item.stat().st_size,
                        "sha256": digest_file(item)})
    return digest_json(entries)


def redact_sensitive(value: Any) -> Any:
    """Remove reusable local ownership/launch secrets while retaining evidence shape."""

    if isinstance(value, Mapping):
        redacted = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"owner_token", "launch_secret", "private_key", "secret"}:
                if (isinstance(child, str) and child.startswith("sha256:")
                        and len(child) == 71
                        and all(character in "0123456789abcdef" for character in child[7:])):
                    redacted[key] = child
                else:
                    encoded = str(child).encode("utf-8")
                    redacted[key] = "sha256:" + hashlib.sha256(encoded).hexdigest()
            else:
                redacted[key] = redact_sensitive(child)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(child) for child in value]
    return value


def metric(value: int | float | None, unit: str, source: str, *,
           reason: str | None = None, status: str | None = None) -> dict[str, Any]:
    if value is None and not reason:
        raise ValueError("a missing metric requires a reason")
    return {
        "value": value,
        "unit": unit,
        "source": source,
        "status": status or ("measured" if value is not None else "not_measured"),
        "reason": reason,
    }


def not_applicable(unit: str, reason: str) -> dict[str, Any]:
    return metric(None, unit, "provider_contract", reason=reason, status="not_applicable")


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    create_time: float | None
    rss_bytes: int | None
    fd_count: int | None
    ipc_connection_count: int | None
    status: str | None
    measurement_errors: tuple[str, ...]


@dataclass(frozen=True)
class ProcessTreeSample:
    timestamp_utc: str
    monotonic_ns: int
    root_pid: int
    expected_root_create_time: float | None
    identity_match: bool | None
    processes: tuple[ProcessEntry, ...]
    process_count: dict[str, Any]
    child_count: dict[str, Any]
    rss_bytes: dict[str, Any]
    fd_count: dict[str, Any]
    ipc_connection_count: dict[str, Any]
    source: str
    sampling_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _process_create_time(process: Any) -> tuple[float | None, str | None]:
    try:
        return float(process.create_time()), None
    except Exception as exc:  # pragma: no cover - process may disappear mid-sample
        return None, f"create_time:{type(exc).__name__}"


def _process_entry(process: Any) -> ProcessEntry:
    errors: list[str] = []
    create_time, create_error = _process_create_time(process)
    if create_error:
        errors.append(create_error)
    try:
        rss = int(process.memory_info().rss)
    except Exception as exc:
        rss = None
        errors.append(f"rss:{type(exc).__name__}")
    try:
        if hasattr(process, "num_fds"):
            fds = int(process.num_fds())
        elif hasattr(process, "num_handles"):
            fds = int(process.num_handles())
        else:
            fds = None
            errors.append("fd_count:unsupported")
    except Exception as exc:
        fds = None
        errors.append(f"fd_count:{type(exc).__name__}")
    try:
        connections = process.net_connections(kind="all")
        ipc_connections = sum(
            1 for connection in connections if getattr(connection, "family", None) == socket.AF_UNIX
        )
    except Exception as exc:
        ipc_connections = None
        errors.append(f"ipc_connections:{type(exc).__name__}")
    try:
        process_status = str(process.status())
    except Exception as exc:
        process_status = None
        errors.append(f"status:{type(exc).__name__}")
    return ProcessEntry(
        pid=int(process.pid), create_time=create_time, rss_bytes=rss, fd_count=fds,
        ipc_connection_count=ipc_connections, status=process_status,
        measurement_errors=tuple(errors),
    )


def _sum_complete(entries: Sequence[ProcessEntry], field: str, unit: str) -> dict[str, Any]:
    values = [getattr(entry, field) for entry in entries]
    if not entries:
        return metric(None, unit, "psutil", reason="process_tree_empty")
    missing = [entry.pid for entry, value in zip(entries, values) if value is None]
    if missing:
        return metric(None, unit, "psutil",
                      reason="counter_missing_for_pids:" + ",".join(map(str, missing)))
    return metric(sum(int(value) for value in values), unit, "psutil")


def sample_process_tree(root_pid: int, *,
                        expected_create_time: float | None = None) -> ProcessTreeSample:
    """Sample a real owned process tree and retain PID/create-time identity."""

    timestamp, monotonic_ns = utc_now(), time.monotonic_ns()
    if psutil is None:
        count = metric(None, "count", "not_measured", reason="psutil_unavailable")
        return ProcessTreeSample(
            timestamp, monotonic_ns, int(root_pid), expected_create_time, None, (), count, count,
            metric(None, "bytes", "not_measured", reason="psutil_unavailable"), count, count,
            "not_measured", ("psutil_unavailable",),
        )
    errors: list[str] = []
    try:
        root = psutil.Process(int(root_pid))
        actual_create_time, create_error = _process_create_time(root)
        if create_error:
            errors.append(create_error)
        identity_match = None
        if expected_create_time is not None and actual_create_time is not None:
            identity_match = abs(actual_create_time - float(expected_create_time)) < 0.01
            if not identity_match:
                errors.append("root_identity_mismatch")
        process_objects = [root]
        try:
            process_objects.extend(root.children(recursive=True))
        except Exception as exc:
            errors.append(f"children:{type(exc).__name__}")
        unique = {int(process.pid): process for process in process_objects}
        entries = tuple(_process_entry(unique[pid]) for pid in sorted(unique))
        complete = not any(error.startswith("children:") for error in errors)
        process_count = metric(len(entries) if complete else None, "count", "psutil",
                               reason=None if complete else "recursive_children_incomplete")
        child_count = metric(len(entries) - 1 if complete else None, "count", "psutil",
                             reason=None if complete else "recursive_children_incomplete")
        return ProcessTreeSample(
            timestamp, monotonic_ns, int(root_pid), expected_create_time, identity_match, entries,
            process_count, child_count, _sum_complete(entries, "rss_bytes", "bytes"),
            _sum_complete(entries, "fd_count", "count"),
            _sum_complete(entries, "ipc_connection_count", "count"), "psutil", tuple(errors),
        )
    except Exception as exc:
        reason = f"root_process_unavailable:{type(exc).__name__}"
        return ProcessTreeSample(
            timestamp, monotonic_ns, int(root_pid), expected_create_time, None, (),
            metric(None, "count", "psutil", reason=reason),
            metric(None, "count", "psutil", reason=reason),
            metric(None, "bytes", "psutil", reason=reason),
            metric(None, "count", "psutil", reason=reason),
            metric(None, "count", "psutil", reason=reason), "psutil", (reason,),
        )


def process_create_time(pid: int) -> float | None:
    if psutil is None:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _find_numeric(value: Any, names: Iterable[str]) -> int | float | None:
    wanted = set(names)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in wanted and type(child) in (int, float) and child >= 0:
                return child
        for child in value.values():
            found = _find_numeric(child, wanted)
            if found is not None:
                return found
    return None


def _record_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if hasattr(record, "__dataclass_fields__"):
        return asdict(record)
    if isinstance(record, Mapping):
        return dict(record)
    return {name: getattr(record, name) for name in dir(record)
            if not name.startswith("_") and not callable(getattr(record, name, None))}


def sample_supervisor(supervisor: Any, *, include_heartbeat: bool = True) -> dict[str, Any]:
    """Collect one timestamped resource sample through public Supervisor state."""

    record = _record_dict(getattr(supervisor, "record", None))
    pid = record.get("pid") or record.get("provider_pid")
    expected_create_time = record.get("start_time") or record.get("create_time")
    tree = (sample_process_tree(int(pid), expected_create_time=expected_create_time).to_dict()
            if type(pid) is int and pid > 0 else None)
    status: Any = {}
    heartbeat: Any = {}
    status_error = heartbeat_error = None
    try:
        if callable(getattr(supervisor, "status", None)):
            status = supervisor.status()
    except Exception as exc:
        status_error = type(exc).__name__
    if include_heartbeat:
        try:
            if callable(getattr(supervisor, "heartbeat", None)):
                heartbeat = supervisor.heartbeat()
            elif callable(getattr(supervisor, "call", None)):
                heartbeat = supervisor.call("heartbeat", {}, timeout=5)
        except Exception as exc:
            heartbeat_error = type(exc).__name__
    observed = {"status": _mapping(status), "heartbeat": _mapping(heartbeat)}
    queue = _find_numeric(observed, ("queue", "queued", "queue_depth"))
    inflight = _find_numeric(observed, ("inflight", "in_flight", "running"))
    connections = _find_numeric(observed, ("connections", "connection_count", "ipc_connections"))
    try:
        owned = (supervisor.owned_resources()
                 if callable(getattr(supervisor, "owned_resources", None)) else None)
    except Exception as exc:
        owned = None
        status_error = status_error or f"owned_resources:{type(exc).__name__}"
    missing_reason = status_error or heartbeat_error or "counter_not_reported"
    return {
        "timestamp_utc": utc_now(), "monotonic_ns": time.monotonic_ns(),
        "record": redact_sensitive(record),
        "process_tree": tree,
        "queue_depth": metric(queue, "count", "supervisor_status_or_heartbeat",
                              reason=None if queue is not None else missing_reason),
        "inflight": metric(inflight, "count", "supervisor_status_or_heartbeat",
                           reason=None if inflight is not None else missing_reason),
        "ipc_connections": metric(connections, "count", "supervisor_status_or_heartbeat",
                                  reason=None if connections is not None else missing_reason),
        "provider_db_connections": not_applicable(
            "count", "fake_provider_has_no_database_driver"),
        "owned_resources": redact_sensitive(owned), "status_error": status_error,
        "heartbeat_error": heartbeat_error,
    }


def timed(callable_: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    started_ns, started_utc = time.monotonic_ns(), utc_now()
    value = callable_()
    ended_ns = time.monotonic_ns()
    return value, {
        "started_at_utc": started_utc, "ended_at_utc": utc_now(),
        "started_monotonic_ns": started_ns, "ended_monotonic_ns": ended_ns,
        "duration_ms": round((ended_ns - started_ns) / 1_000_000, 6),
        "clock": "time.monotonic_ns", "unit": "milliseconds",
    }


def percentile(values: Sequence[float], percentile_value: float) -> float | None:
    """Nearest-rank percentile, with the method frozen in the profile."""

    if not values:
        return None
    if not 0 < percentile_value <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, math.ceil(percentile_value / 100 * len(ordered)) - 1)]


def summarize(values: Sequence[int | float]) -> dict[str, Any]:
    numeric = sorted(float(value) for value in values)
    if not numeric:
        return {"count": 0, "median": None, "p95": None, "max": None}
    middle = len(numeric) // 2
    median = (numeric[middle] if len(numeric) % 2
              else (numeric[middle - 1] + numeric[middle]) / 2)
    return {"count": len(numeric), "median": median,
            "p95": percentile(numeric, 95), "max": numeric[-1]}


def linear_slope(values: Sequence[int | float]) -> float | None:
    """Least-squares slope per sample index, retained as evidence not a leak verdict."""

    numeric = [float(value) for value in values]
    if len(numeric) < 2:
        return None
    mean_x = (len(numeric) - 1) / 2
    mean_y = sum(numeric) / len(numeric)
    denominator = sum((index - mean_x) ** 2 for index in range(len(numeric)))
    if denominator == 0:
        return None
    return sum((index - mean_x) * (value - mean_y)
               for index, value in enumerate(numeric)) / denominator


def current_python_abi() -> str:
    return str(getattr(sys.implementation, "cache_tag", None) or platform.python_implementation())


def base_context() -> dict[str, Any]:
    uname = platform.uname()
    return {
        "os": {"system": uname.system, "release": uname.release, "version": uname.version},
        "cpu": {"machine": uname.machine, "processor": uname.processor or None},
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(), "abi": current_python_abi(),
            "executable_sha256": digest_file(Path(sys.executable)),
        },
    }


def _without_profile_digest(profile: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(profile)
    value.pop("profile_digest", None)
    return value


def freeze_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    frozen = json.loads(json.dumps(dict(profile)))
    frozen.update({"schema": PROFILE_SCHEMA, "fixture_only": True,
                   "qa_ops_acceptance": "pending"})
    frozen["profile_digest"] = digest_json(_without_profile_digest(frozen))
    return frozen


def _positive_int(value: Any, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise CapacityEvidenceError("profile_sample_requirement_invalid", name)
    return value


def validate_frozen_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise CapacityEvidenceError("profile_invalid", "root_not_object")
    if profile.get("schema") != PROFILE_SCHEMA:
        raise CapacityEvidenceError("profile_schema_invalid")
    if profile.get("fixture_only") is not True or profile.get("qa_ops_acceptance") != "pending":
        raise CapacityEvidenceError("profile_scope_invalid")
    if profile.get("profile_digest") != digest_json(_without_profile_digest(profile)):
        raise CapacityEvidenceError("profile_digest_mismatch")
    kind = profile.get("kind")
    if kind not in {"single", "dual"}:
        raise CapacityEvidenceError("profile_kind_invalid")
    for field in ("context", "packages", "load", "thresholds", "required_metrics", "statistics"):
        if not profile.get(field):
            raise CapacityEvidenceError("profile_field_missing", field)
    for field in ("calibration_raw_path", "calibration_raw_digest"):
        if not profile.get(field):
            raise CapacityEvidenceError("profile_field_missing", field)
    required_context_paths = (
        "machine.os.system", "machine.os.release", "machine.cpu.machine",
        "machine.python.version", "machine.python.abi", "engine_runtime.host_digest",
        "engine_runtime.sdk_digest", "engine_runtime.launcher_digest",
    )
    for path in required_context_paths:
        if not get_path(profile["context"], path):
            raise CapacityEvidenceError("profile_context_field_missing", path)
    required_package_fields = (
        "version", "source_digest", "package_digest", "manifest_sha256", "payload_digest",
        "runtime_digest", "artifact_path", "artifact_sha256",
    )
    for index, package in enumerate(profile.get("packages", [])):
        if not isinstance(package, Mapping):
            raise CapacityEvidenceError("profile_package_invalid", str(index))
        for field in required_package_fields:
            if not package.get(field):
                raise CapacityEvidenceError("profile_package_field_missing", f"{index}.{field}")
    requirements = profile.get("sample_requirements")
    if not isinstance(requirements, Mapping):
        raise CapacityEvidenceError("profile_field_missing", "sample_requirements")
    if kind == "dual":
        _positive_int(requirements.get("dual_cycles"), "dual_cycles", 20)
        if len(profile.get("packages", [])) != 2:
            raise CapacityEvidenceError("profile_package_count_invalid", "dual_requires_two")
        if not profile.get("single_profile_digest"):
            raise CapacityEvidenceError("profile_field_missing", "single_profile_digest")
        if not profile.get("single_profile_path"):
            raise CapacityEvidenceError("profile_field_missing", "single_profile_path")
        for field in ("single_candidate_path", "single_candidate_raw_digest"):
            if not profile.get(field):
                raise CapacityEvidenceError("profile_field_missing", field)
    else:
        _positive_int(requirements.get("cold_starts"), "cold_starts", 30)
        _positive_int(requirements.get("warm_starts"), "warm_starts", 30)
        _positive_int(requirements.get("invoke_read"), "invoke_read", 200)
        _positive_int(requirements.get("lifecycle_rounds"), "lifecycle_rounds", 20)
        if len(profile.get("packages", [])) != 1:
            raise CapacityEvidenceError("profile_package_count_invalid", "single_requires_one")
    thresholds = profile.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise CapacityEvidenceError("profile_thresholds_invalid")
    for name, threshold in thresholds.items():
        if not isinstance(name, str) or not isinstance(threshold, Mapping):
            raise CapacityEvidenceError("profile_threshold_invalid", str(name))
        if threshold.get("comparison") not in {"max", "min", "equal"}:
            raise CapacityEvidenceError("profile_threshold_invalid", name)
        value = threshold.get("value")
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise CapacityEvidenceError("profile_threshold_invalid", name)
        if not threshold.get("unit") or "allowed_margin" not in threshold:
            raise CapacityEvidenceError("profile_threshold_invalid", name)
        margin = threshold.get("allowed_margin")
        if type(margin) not in (int, float) or not math.isfinite(margin) or margin < 0:
            raise CapacityEvidenceError("profile_threshold_invalid", name)
    if not all(isinstance(item, str) and item for item in profile.get("required_metrics", [])):
        raise CapacityEvidenceError("profile_required_metrics_invalid")
    required = set(profile["required_metrics"])
    missing_required = REQUIRED_METRICS_BY_KIND[kind] - required
    missing_thresholds = REQUIRED_METRICS_BY_KIND[kind] - set(thresholds)
    if missing_required:
        raise CapacityEvidenceError("profile_required_metrics_missing", sorted(missing_required)[0])
    if missing_thresholds:
        raise CapacityEvidenceError("profile_thresholds_missing", sorted(missing_thresholds)[0])
    return dict(profile)


def _diff_exact(expected: Any, actual: Any, prefix: str = "") -> list[str]:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        differences: list[str] = []
        for key in sorted(set(expected) - set(actual)):
            differences.append(f"{prefix}{key}:missing")
        for key in sorted(set(actual) - set(expected)):
            differences.append(f"{prefix}{key}:unexpected")
        for key in sorted(set(expected) & set(actual)):
            differences.extend(_diff_exact(expected[key], actual[key], f"{prefix}{key}."))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        return [] if expected == actual else [f"{prefix[:-1]}:mismatch"]
    return [] if expected == actual else [f"{prefix[:-1]}:mismatch"]


def formal_preflight(profile: Mapping[str, Any], *, kind: str,
                     context: Mapping[str, Any], packages: Sequence[Mapping[str, Any]],
                     load: Mapping[str, Any],
                     single_profile_digest: str | None = None) -> dict[str, Any]:
    """Validate every frozen binding before any Supervisor is constructed."""

    frozen = validate_frozen_profile(profile)
    if frozen["kind"] != kind:
        raise CapacityEvidenceError("profile_kind_mismatch")
    for name, expected, actual in (
        ("context", frozen["context"], context),
        ("packages", frozen["packages"], list(packages)),
        ("load", frozen["load"], load),
    ):
        differences = _diff_exact(expected, actual)
        if differences:
            raise CapacityEvidenceError(f"profile_{name}_mismatch", ",".join(differences[:20]))
    if kind == "dual" and frozen.get("single_profile_digest") != single_profile_digest:
        raise CapacityEvidenceError("single_profile_digest_mismatch")
    return frozen


def get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def evaluate_report(report: Mapping[str, Any], profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return missing-required and budget failures from raw-derived summaries."""

    frozen = validate_frozen_profile(profile)
    failures: list[dict[str, Any]] = []
    for path in frozen["required_metrics"]:
        if get_path(report, path) is None:
            failures.append({"code": "required_metric_missing", "metric": path})
    for path, threshold in frozen["thresholds"].items():
        actual = get_path(report, path)
        if type(actual) not in (int, float) or not math.isfinite(actual):
            failures.append({"code": "threshold_metric_missing", "metric": path})
            continue
        expected, comparison = threshold["value"], threshold["comparison"]
        passed = (actual <= expected if comparison == "max" else
                  actual >= expected if comparison == "min" else actual == expected)
        if not passed:
            failures.append({"code": "budget_exceeded", "metric": path,
                             "comparison": comparison, "actual": actual,
                             "threshold": expected, "unit": threshold["unit"]})
    return failures


def assert_raw_counts(raw: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
    requirements = validate_frozen_profile(profile)["sample_requirements"]
    observed = raw.get("sample_counts") if isinstance(raw, Mapping) else None
    if not isinstance(observed, Mapping):
        raise CapacityEvidenceError("raw_sample_counts_missing")
    for name, required in requirements.items():
        if name == "dual_cycles" and profile.get("kind") != "dual":
            continue
        count = observed.get(name)
        if type(count) is not int or count < required:
            raise CapacityEvidenceError("raw_sample_count_insufficient", name)


# Backward-compatible narrow shape used by the first implementation/tests.
@dataclass(frozen=True)
class Sample:
    pid: int
    rss_bytes: int | None
    children: int | None
    fds: int | None
    source: str


def sample_process(pid: int) -> Sample:
    tree = sample_process_tree(pid)
    return Sample(pid=int(pid), rss_bytes=tree.rss_bytes["value"],
                  children=tree.child_count["value"], fds=tree.fd_count["value"],
                  source=tree.source)
