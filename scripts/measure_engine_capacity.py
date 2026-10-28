#!/usr/bin/env python3
"""Calibrate and formally measure the real fixture Supervisor path.

Fixture-only candidate generator: it never connects to a database, signs
C80/C81, or writes production output instances. Formal preflight completes
before a work directory or Supervisor is created.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.engine_host.metrics import (  # noqa: E402
    RAW_SCHEMA, REPORT_SCHEMA, CapacityEvidenceError, assert_raw_counts, base_context,
    digest_directory, digest_file, digest_json, evaluate_report, formal_preflight, freeze_profile, get_path,
    linear_slope, metric, process_create_time, redact_sensitive, sample_process_tree,
    sample_supervisor, summarize, timed,
    utc_now, validate_frozen_profile,
)
from app.runtime.engine_host.supervisor import FixtureSupervisor  # noqa: E402


MIN_COLD = 30
MIN_WARM = 30
MIN_INVOKE = 200
MIN_LIFECYCLE = 20
MIN_DUAL_CYCLES = 20
INVOKE_CASES = (
    {"name": "concurrency_1", "concurrency": 1, "calls": 64, "sleep_seconds": 0.0},
    {"name": "concurrency_4", "concurrency": 4, "calls": 64, "sleep_seconds": 0.002},
    {"name": "concurrency_8", "concurrency": 8, "calls": 96, "sleep_seconds": 0.002},
    {"name": "over_limit", "concurrency": 32, "calls": 48, "sleep_seconds": 0.05,
     "expect_rejection": True},
)
LIMITS = {"max_queue": 16, "max_inflight": 8, "max_connections": 32}
STATISTICS = {"median": "sorted-middle-average", "p95": "nearest-rank-ceil",
              "max": "observed-maximum", "duration_clock": "time.monotonic_ns"}


def _json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(redact_sensitive(value), ensure_ascii=False,
                                    sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def _json_read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except Exception as exc:
        raise CapacityEvidenceError("profile_read_failed", type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise CapacityEvidenceError("profile_invalid", "root_not_object")
    return value


def _verify_calibration_evidence(profile: Mapping[str, Any]) -> dict[str, Any]:
    raw_path = Path(str(profile.get("calibration_raw_path", ""))).resolve()
    if not raw_path.is_file():
        raise CapacityEvidenceError("calibration_raw_missing", str(raw_path))
    raw = _json_read(raw_path)
    if digest_json(raw) != profile.get("calibration_raw_digest"):
        raise CapacityEvidenceError("calibration_raw_digest_mismatch")
    if raw.get("schema") != RAW_SCHEMA or raw.get("kind") != profile.get("kind"):
        raise CapacityEvidenceError("calibration_raw_identity_mismatch")
    assert_raw_counts(raw, profile)
    if profile["kind"] == "single":
        summary = _single_summary(raw)
        for path, threshold in profile["thresholds"].items():
            summary_path = path.removeprefix("metrics.")
            observed = get_path(summary, summary_path)
            if observed != threshold.get("calibration_observed"):
                raise CapacityEvidenceError("calibration_statistic_mismatch", path)
    return raw


def _validate_single_candidate(profile: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if (candidate.get("schema") != REPORT_SCHEMA or candidate.get("kind") != "single"
            or candidate.get("phase") != "formal" or candidate.get("fixture_only") is not True
            or candidate.get("qa_ops_acceptance") != "pending"
            or candidate.get("status") != "candidate" or candidate.get("ok") is not True
            or candidate.get("failures")):
        raise CapacityEvidenceError("single_candidate_invalid")
    if candidate.get("profile_digest") != profile.get("profile_digest"):
        raise CapacityEvidenceError("single_candidate_profile_mismatch")
    for key in ("context", "packages", "load"):
        if candidate.get(key) != profile.get(key):
            raise CapacityEvidenceError("single_candidate_binding_mismatch", key)
    raw = candidate.get("raw")
    if not isinstance(raw, Mapping) or candidate.get("raw_digest") != digest_json(raw):
        raise CapacityEvidenceError("single_candidate_raw_digest_mismatch")
    assert_raw_counts(raw, profile)
    if evaluate_report(candidate, profile):
        raise CapacityEvidenceError("single_candidate_budget_failed")
    return dict(candidate)


def _package_api():
    try:
        from app.runtime.engine_host import packages
    except Exception as exc:
        raise CapacityEvidenceError("package_api_unavailable", type(exc).__name__) from exc
    for name in ("build_fixture_package", "save_package", "load_package", "describe_context"):
        if not callable(getattr(packages, name, None)):
            raise CapacityEvidenceError("package_api_incomplete", name)
    return packages


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {key: child for key, child in vars(value).items() if not key.startswith("_")}
    return {}


def _package_identity(package: Any, *, artifact_path: Path) -> dict[str, Any]:
    verified = package.verify() if callable(getattr(package, "verify", None)) else {}
    verified_mapping = _as_mapping(verified)
    manifest = getattr(package, "manifest", None)
    if not isinstance(manifest, Mapping):
        raise CapacityEvidenceError("package_manifest_missing")
    source = Path(getattr(package, "source", ""))
    if not source.is_dir():
        raise CapacityEvidenceError("package_source_missing")
    identity = {
        "version": str(getattr(package, "version", manifest.get("version", ""))),
        "source_digest": str(getattr(package, "source_digest", "")
                             or verified_mapping.get("source_digest", "")
                             or digest_directory(source)),
        "package_digest": str(getattr(package, "digest", "")),
        "manifest_sha256": str(verified_mapping.get("manifest_sha256")
                               or getattr(package, "manifest_sha256", "")
                               or digest_json(manifest)),
        "payload_digest": str(getattr(package, "payload_digest", "")
                              or verified_mapping.get("payload_digest", "")),
        "runtime_digest": str(getattr(package, "runtime_digest", "")
                              or verified_mapping.get("runtime_digest", "")),
        "artifact_path": str(artifact_path.resolve()),
        "artifact_sha256": digest_file(artifact_path),
    }
    required = ("version", "source_digest", "package_digest", "manifest_sha256",
                "payload_digest", "runtime_digest")
    if not all(identity[field] for field in required):
        raise CapacityEvidenceError("package_identity_incomplete")
    return identity


def _build_saved_package(*, api: Any, build_root: Path, artifacts: Path,
                         version: str, index: int) -> tuple[Any, dict[str, Any]]:
    package = api.build_fixture_package(build_root / f"package-build-{index}", version=version)
    artifact = artifacts / f"fixture-{version}.json"
    if artifact.exists():
        raise CapacityEvidenceError("package_artifact_already_exists", str(artifact))
    api.save_package(package, artifact)
    loaded = api.load_package(artifact, expected_digest=getattr(package, "digest", None))
    return loaded, _package_identity(loaded, artifact_path=artifact)


def _save_packages(*, workdir: Path, report: Path,
                   versions: Sequence[str]) -> tuple[list[Any], list[dict[str, Any]]]:
    api = _package_api()
    loaded_packages, identities = [], []
    artifacts = report.parent / "capacity-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for index, version in enumerate(versions, 1):
        loaded, identity = _build_saved_package(
            api=api, build_root=artifacts / "payloads", artifacts=artifacts,
            version=version, index=index)
        loaded_packages.append(loaded)
        identities.append(identity)
    return loaded_packages, identities


def _load_packages(identities: Sequence[Mapping[str, Any]]) -> list[Any]:
    api = _package_api()
    loaded = []
    for identity in identities:
        artifact = Path(str(identity.get("artifact_path", ""))).resolve()
        if not artifact.is_file():
            raise CapacityEvidenceError("package_artifact_missing", str(artifact))
        if digest_file(artifact) != identity.get("artifact_sha256"):
            raise CapacityEvidenceError("package_artifact_digest_mismatch", str(artifact))
        package = api.load_package(artifact, expected_digest=identity.get("package_digest"))
        if _package_identity(package, artifact_path=artifact) != dict(identity):
            raise CapacityEvidenceError("package_identity_mismatch", str(artifact))
        loaded.append(package)
    return loaded


def _context() -> dict[str, Any]:
    described = _package_api().describe_context()
    if not isinstance(described, Mapping):
        raise CapacityEvidenceError("context_invalid")
    return {"machine": base_context(), "engine_runtime": dict(described),
            "measurement_tool_sha256": digest_file(Path(__file__))}


def _load(samples: int, kind: str) -> dict[str, Any]:
    common = {"kind": kind, "limits": dict(LIMITS), "sample_interval_ms": 10,
              "driver": "fake/none", "clear_os_page_cache": False}
    if kind == "dual":
        return {**common, "dual_cycles": MIN_DUAL_CYCLES,
                "flow": "v1_to_v2_to_safe_rollback_with_persistent_orchestrator"}
    return {**common, "cold_starts": samples, "warm_starts": samples,
            "invoke_read": {"minimum_successful": MIN_INVOKE, "cases": list(INVOKE_CASES)},
            "lifecycle_rounds": MIN_LIFECYCLE, "dual_cycles": 0}


def _requirements(samples: int, kind: str) -> dict[str, int]:
    if kind == "dual":
        return {"dual_cycles": MIN_DUAL_CYCLES}
    return {"cold_starts": samples, "warm_starts": samples,
            "invoke_read": MIN_INVOKE, "lifecycle_rounds": MIN_LIFECYCLE}


def _new_supervisor(workdir: Path, *, limits: Mapping[str, int] = LIMITS) -> Any:
    parameters = inspect.signature(FixtureSupervisor).parameters
    kwargs: dict[str, Any] = {"workdir": workdir}
    for name, value in limits.items():
        if name not in parameters:
            raise CapacityEvidenceError("supervisor_capacity_limit_unsupported", name)
        kwargs[name] = value
    return FixtureSupervisor(**kwargs)


def _start(supervisor: Any, package: Any, *, generation: int = 1) -> Any:
    manifest = getattr(package, "manifest")
    parameters = inspect.signature(supervisor.start).parameters
    kwargs: dict[str, Any] = {
        "expected_namespace": manifest.get("namespace", "tmengine.test"),
        "expected_channel": manifest.get("channel", "test"),
    }
    if "generation" in parameters:
        kwargs["generation"] = generation
    return supervisor.start(getattr(package, "source"), manifest, getattr(package, "signature"),
                            getattr(package, "trusted_keys"), **kwargs)


def _drain(supervisor: Any, timeout: float = 10) -> Any:
    if callable(getattr(supervisor, "drain", None)):
        return supervisor.drain(timeout=timeout)
    return supervisor.call("drain", {}, timeout=timeout)


def _cleanup(supervisor: Any) -> Any:
    try:
        value = supervisor.cleanup_report() if callable(
            getattr(supervisor, "cleanup_report", None)) else None
        return redact_sensitive(value)
    except Exception as exc:
        return {"error": type(exc).__name__}


def _residual(record: Any) -> dict[str, Any]:
    mapping = _as_mapping(record)
    pid = mapping.get("pid") or mapping.get("provider_pid")
    expected = mapping.get("start_time") or mapping.get("create_time")
    alive, identity_match = False, None
    if type(pid) is int and pid > 0:
        actual = process_create_time(pid)
        alive = actual is not None
        if actual is not None:
            identity_match = (abs(float(expected) - actual) < 0.01
                              if expected is not None else True)
    socket_path = mapping.get("socket_path")
    socket_exists = bool(socket_path and Path(str(socket_path)).exists())
    return {"pid": pid, "expected_create_time": expected, "alive": alive,
            "identity_match": identity_match, "socket_path": socket_path,
            "socket_exists": socket_exists,
            "residual_count": int(alive and identity_match is not False) + int(socket_exists),
            "source": "psutil_pid_create_time_and_filesystem", "unit": "count"}


def _measure_starts(package: Any, root: Path, *, count: int,
                    mode: str) -> list[dict[str, Any]]:
    if mode == "warm":
        return _measure_warm_starts(package, root, count=count)
    samples: list[dict[str, Any]] = []
    identity = {"package_digest": getattr(package, "digest", None),
                "payload_digest": getattr(package, "payload_digest", None),
                "runtime_digest": getattr(package, "runtime_digest", None)}
    immutable_source_digest = None
    if mode == "warm":
        if not callable(getattr(package, "verify", None)):
            raise CapacityEvidenceError("warm_package_verify_unavailable")
        package.verify()
        immutable_source_digest = digest_directory(Path(package.source))
    for index in range(count):
        supervisor = _new_supervisor(root / f"{mode}-{index:03d}")
        record = None
        try:
            record, start_timing = timed(lambda: _start(supervisor, package))
            selftest, selftest_timing = timed(lambda: supervisor.call("selftest", {}, timeout=10))
            samples.append({
                "index": index, "mode": mode,
                "definition": ("new_private_staging_and_new_process" if mode == "cold"
                               else "same_preverified_immutable_payload_and_new_process"),
                "preverified_source_digest": immutable_source_digest,
                "os_page_cache_cleared": False, "package": identity,
                "record": redact_sensitive(_as_mapping(record)), "start": start_timing,
                "selftest": selftest_timing, "selftest_result": selftest,
                "resource": sample_supervisor(supervisor), "error": None,
            })
        except Exception as exc:
            samples.append({"index": index, "mode": mode, "package": identity,
                            "error": getattr(exc, "code", type(exc).__name__)})
            raise
        finally:
            supervisor.stop()
            samples[-1]["after_stop"] = _residual(record)
            samples[-1]["cleanup_report"] = _cleanup(supervisor)
    if mode == "warm" and digest_directory(Path(package.source)) != immutable_source_digest:
        raise CapacityEvidenceError("warm_immutable_payload_changed")
    if len({sample["record"]["launch_instance_id"] for sample in samples}) != count:
        raise CapacityEvidenceError(f"{mode}_launch_identity_reused")
    return samples


def _measure_warm_starts(package: Any, root: Path, *, count: int) -> list[dict[str, Any]]:
    """Use one verified staging path while each restart creates a new process/launch."""

    supervisor = _new_supervisor(root / "warm-reused-staging")
    samples: list[dict[str, Any]] = []
    identity = {"package_digest": getattr(package, "digest", None),
                "payload_digest": getattr(package, "payload_digest", None),
                "runtime_digest": getattr(package, "runtime_digest", None)}
    try:
        seed_record = _start(supervisor, package)
        seed_selftest = supervisor.call("selftest", {}, timeout=10)
        seed_staging = _as_mapping(seed_record).get("staging_dir")
        seed = {"record": redact_sensitive(_as_mapping(seed_record)),
                "selftest_result": seed_selftest,
                "purpose": "verify_and_stage_seed_not_counted_as_warm_sample"}
        previous = seed_record
        for index in range(count):
            _, previous_drain = timed(lambda: _drain(supervisor))
            record, start_timing = timed(supervisor.restart)
            selftest, selftest_timing = timed(
                lambda: supervisor.call("selftest", {}, timeout=10))
            record_mapping = _as_mapping(record)
            if record_mapping.get("staging_dir") != seed_staging:
                raise CapacityEvidenceError("warm_staging_path_changed")
            previous_residual = _residual(previous)
            previous_residual["socket_reused_by_new_launch"] = (
                _as_mapping(previous).get("socket_path") == record_mapping.get("socket_path"))
            previous_residual["residual_count"] = int(
                previous_residual["alive"] and previous_residual["identity_match"] is not False)
            samples.append({
                "index": index, "mode": "warm",
                "definition": "same_verified_immutable_staging_and_new_process",
                "os_page_cache_cleared": False, "package": identity, "seed": seed,
                "record": redact_sensitive(record_mapping),
                "previous_after_restart": previous_residual,
                "previous_drain": previous_drain, "start": start_timing,
                "selftest": selftest_timing, "selftest_result": selftest,
                "resource": sample_supervisor(supervisor), "error": None,
            })
            previous = record
    except Exception as exc:
        samples.append({"index": len(samples), "mode": "warm", "package": identity,
                        "error": getattr(exc, "code", type(exc).__name__)})
        raise
    finally:
        final_record = supervisor.record
        supervisor.stop()
        if samples:
            samples[-1]["after_stop"] = _residual(final_record)
            samples[-1]["cleanup_report"] = _cleanup(supervisor)
    if len(samples) != count:
        raise CapacityEvidenceError("warm_sample_count_incomplete")
    if len({sample["record"]["launch_instance_id"] for sample in samples}) != count:
        raise CapacityEvidenceError("warm_launch_identity_reused")
    if any(sample["previous_after_restart"]["residual_count"] for sample in samples):
        raise CapacityEvidenceError("warm_previous_launch_not_drained")
    return samples


def _invoke_one(supervisor: Any, case: Mapping[str, Any], index: int) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        result = supervisor.call("invoke_read", {"echo": f"{case['name']}-{index}",
                                 "sleep": case["sleep_seconds"]}, timeout=10)
        return {"index": index, "ok": True, "rejected": False,
                "duration_ms": (time.monotonic_ns() - started) / 1_000_000,
                "result_identity": {"marker": result.get("marker"),
                                    "provider_version": result.get("provider_version")}}
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        rejected = code in {"queue_full", "inflight_full", "connection_limit", "draining"}
        return {"index": index, "ok": False, "rejected": rejected, "error": code,
                "duration_ms": (time.monotonic_ns() - started) / 1_000_000}


def _measure_invokes(package: Any, root: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    total_successful = 0
    for case in INVOKE_CASES:
        supervisor = _new_supervisor(root / case["name"])
        stop_sampling = threading.Event()
        timeline: list[dict[str, Any]] = []
        record = None
        try:
            record = _start(supervisor, package)

            def sampler() -> None:
                while not stop_sampling.wait(0.01):
                    timeline.append(sample_supervisor(supervisor, include_heartbeat=False))

            sampling_thread = threading.Thread(target=sampler, daemon=True)
            sampling_thread.start()
            with ThreadPoolExecutor(max_workers=int(case["concurrency"])) as executor:
                futures = [executor.submit(_invoke_one, supervisor, case, index)
                           for index in range(int(case["calls"]))]
                samples = [future.result() for future in as_completed(futures)]
            stop_sampling.set()
            sampling_thread.join(timeout=2)
            timeline.append(sample_supervisor(supervisor))
            successful = sum(1 for sample in samples if sample["ok"])
            rejected = sum(1 for sample in samples if sample["rejected"])
            if not case.get("expect_rejection"):
                total_successful += successful
            cases.append({**dict(case), "successful": successful, "rejected": rejected,
                          "failed": len(samples) - successful - rejected,
                          "samples": sorted(samples, key=lambda item: item["index"]),
                          "resource_timeline": timeline})
        finally:
            stop_sampling.set()
            supervisor.stop()
            if cases:
                cases[-1]["after_stop"] = _residual(record)
                cases[-1]["cleanup_report"] = _cleanup(supervisor)
    return {"cases": cases, "successful_regular_calls": total_successful}


def _measure_lifecycle(package: Any, root: Path) -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for index in range(MIN_LIFECYCLE):
        supervisor = _new_supervisor(root / f"round-{index:03d}")
        record = None
        round_started = time.monotonic_ns()
        try:
            record, start_timing = timed(lambda: _start(supervisor, package))
            during_start = sample_supervisor(supervisor)
            result, invoke_timing = timed(
                lambda: supervisor.call("invoke_read", {"echo": f"lifecycle-{index}"}, timeout=10)
            )
            before_drain = sample_supervisor(supervisor)
            drain_result, drain_timing = timed(lambda: _drain(supervisor))
            after_drain = sample_supervisor(supervisor, include_heartbeat=False)
            rounds.append({"index": index, "started_at_utc": start_timing["started_at_utc"],
                           "start": start_timing, "invoke": invoke_timing, "result": result,
                           "drain": drain_timing, "drain_result": drain_result,
                           "resource_timeline": [during_start, before_drain, after_drain]})
        finally:
            _, stop_timing = timed(supervisor.stop)
            if rounds:
                rounds[-1]["stop"] = stop_timing
                rounds[-1]["after_stop"] = _residual(record)
                rounds[-1]["cleanup_report"] = _cleanup(supervisor)
                rounds[-1]["total_duration_ms"] = (
                    time.monotonic_ns() - round_started) / 1_000_000
    return rounds


def _measure_isolation(package: Any, root: Path) -> dict[str, Any]:
    failing = _new_supervisor(root / "timeout-worker")
    healthy = _new_supervisor(root / "healthy-worker")
    try:
        _start(failing, package)
        _start(healthy, package)
        timeout_error: list[str] = []

        def timeout_call() -> None:
            try:
                failing.call("invoke_read", {"sleep": 0.2, "echo": "timeout"}, timeout=0.05)
            except Exception as exc:
                timeout_error.append(getattr(exc, "code", type(exc).__name__))

        thread = threading.Thread(target=timeout_call)
        thread.start()
        healthy_result = healthy.call("invoke_read", {"echo": "healthy"}, timeout=5)
        thread.join(timeout=2)
        return {"timeout_error": timeout_error, "healthy_result": healthy_result,
                "healthy_uninterrupted": healthy_result.get("echo") == "healthy"}
    finally:
        failing.stop()
        healthy.stop()


def _metric_values(raw: Mapping[str, Any], path: Sequence[str]) -> list[float]:
    values: list[float] = []
    resources: list[Any] = []
    for mode in ("cold_starts", "warm_starts"):
        resources.extend(sample.get("resource") for sample in raw.get(mode, []))
    for case in raw.get("invoke", {}).get("cases", []):
        resources.extend(case.get("resource_timeline", []))
    for round_result in raw.get("lifecycle", []):
        resources.extend(round_result.get("resource_timeline", []))
    for resource in resources:
        value: Any = resource
        for part in path:
            value = value.get(part) if isinstance(value, Mapping) else None
        if type(value) in (int, float):
            values.append(float(value))
    return values


def _single_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    cold = [sample["start"]["duration_ms"] for sample in raw["cold_starts"] if not sample["error"]]
    warm = [sample["start"]["duration_ms"] for sample in raw["warm_starts"] if not sample["error"]]
    invoke_cases: dict[str, Any] = {}
    regular: list[float] = []
    for case in raw["invoke"]["cases"]:
        latencies = [sample["duration_ms"] for sample in case["samples"] if sample["ok"]]
        invoke_cases[case["name"]] = {**summarize(latencies), "successful": case["successful"],
                                      "rejected": case["rejected"], "failed": case["failed"]}
        if not case.get("expect_rejection"):
            regular.extend(latencies)
    drains = [item["drain"]["duration_ms"] for item in raw["lifecycle"]]
    stops = [item["stop"]["duration_ms"] for item in raw["lifecycle"]]
    residuals = [item["after_stop"]["residual_count"] for item in raw["lifecycle"]]
    lifecycle_rss = []
    for item in raw["lifecycle"]:
        values = []
        for sample in item["resource_timeline"]:
            value = get_path(sample, "process_tree.rss_bytes.value")
            if type(value) in (int, float):
                values.append(float(value))
        if values:
            lifecycle_rss.append(max(values))
    def described(values: Sequence[float], unit: str, source: str) -> dict[str, Any]:
        return {**summarize(values), "unit": unit, "source": source}

    return {
        "startup": {
            "cold_ms": described(cold, "milliseconds", "raw.cold_starts.start.duration_ms"),
            "warm_ms": described(warm, "milliseconds", "raw.warm_starts.start.duration_ms"),
        },
        "invoke_read": {"combined_regular_ms": described(
                            regular, "milliseconds", "raw.invoke.cases.samples.duration_ms"),
                        "cases": invoke_cases,
                        "successful_regular_calls": raw["invoke"]["successful_regular_calls"]},
        "lifecycle": {"drain_ms": described(
                          drains, "milliseconds", "raw.lifecycle.drain.duration_ms"),
                      "stop_ms": described(
                          stops, "milliseconds", "raw.lifecycle.stop.duration_ms"),
                      "rounds": len(raw["lifecycle"]), "max_residual_count": max(residuals),
                      "rss_peak_by_round": lifecycle_rss,
                      "rss_trend_bytes_per_round": {
                          "value": linear_slope(lifecycle_rss), "unit": "bytes/round",
                          "source": "least_squares_of_raw_lifecycle_round_rss_peaks",
                          "method": "ordinary_least_squares_by_round_index",
                      }},
        "resources": {
            "rss_bytes": described(_metric_values(raw, ("process_tree", "rss_bytes", "value")),
                                    "bytes", "raw.*.resource.process_tree.psutil"),
            "process_count": described(_metric_values(
                raw, ("process_tree", "process_count", "value")), "count", "psutil"),
            "fd_count": described(_metric_values(raw, ("process_tree", "fd_count", "value")),
                                  "count", "psutil"),
            "process_ipc_connections": described(_metric_values(
                raw, ("process_tree", "ipc_connection_count", "value")), "count", "psutil"),
            "queue_depth": described(_metric_values(raw, ("queue_depth", "value")),
                                     "count", "supervisor_status_or_heartbeat"),
            "inflight": described(_metric_values(raw, ("inflight", "value")),
                                  "count", "supervisor_status_or_heartbeat"),
            "ipc_connections": described(_metric_values(raw, ("ipc_connections", "value")),
                                         "count", "supervisor_status_or_heartbeat"),
        },
        "isolation": raw["isolation"],
    }


def _measure_single(package: Any, root: Path, *,
                    samples: int) -> tuple[dict[str, Any], dict[str, Any]]:
    raw: dict[str, Any] = {
        "schema": RAW_SCHEMA, "kind": "single", "started_at_utc": utc_now(),
        "sampling": {"resource_interval_ms": 10, "duration_clock": "time.monotonic_ns",
                     "process_source": "psutil", "queue_source": "supervisor.status/heartbeat"},
    }
    raw["cold_starts"] = _measure_starts(package, root / "cold", count=samples, mode="cold")
    raw["warm_starts"] = _measure_starts(package, root / "warm", count=samples, mode="warm")
    raw["invoke"] = _measure_invokes(package, root / "invoke")
    raw["lifecycle"] = _measure_lifecycle(package, root / "lifecycle")
    raw["isolation"] = _measure_isolation(package, root / "isolation")
    raw["sample_counts"] = {
        "cold_starts": len(raw["cold_starts"]), "warm_starts": len(raw["warm_starts"]),
        "invoke_read": raw["invoke"]["successful_regular_calls"],
        "lifecycle_rounds": len(raw["lifecycle"]),
    }
    raw["completed_at_utc"] = utc_now()
    raw = redact_sensitive(raw)
    return raw, _single_summary(raw)


def _workdir_process_sample(workdir: Path) -> dict[str, Any]:
    """Fallback sampler limited to this runner's workdir-owned descendants."""
    try:
        import psutil
    except Exception:
        return {"timestamp_utc": utc_now(), "source": "not_measured",
                "reason": "psutil_unavailable"}
    roots = []
    for process in psutil.Process(os.getpid()).children(recursive=True):
        try:
            if str(workdir) in "\x00".join(process.cmdline()) or str(workdir) in process.cwd():
                roots.append(process)
        except Exception:
            continue
    root_pids = {process.pid for process in roots}
    top_roots = []
    for process in roots:
        try:
            if process.ppid() not in root_pids:
                top_roots.append(process)
        except Exception:
            top_roots.append(process)
    entries = [sample_process_tree(process.pid, expected_create_time=process.create_time()).to_dict()
               for process in top_roots]

    def total(path: Sequence[str]) -> int | None:
        values = []
        for entry in entries:
            value: Any = entry
            for part in path:
                value = value.get(part) if isinstance(value, Mapping) else None
            if type(value) not in (int, float):
                return None
            values.append(int(value))
        return sum(values) if values else 0

    rss, fds = total(("rss_bytes", "value")), total(("fd_count", "value"))
    process_count = total(("process_count", "value"))
    return {
        "timestamp_utc": utc_now(), "monotonic_ns": time.monotonic_ns(),
        "source": "psutil_descendants_filtered_by_owned_workdir", "process_trees": entries,
        "process_count": metric(process_count, "count", "psutil_owned_workdir_filter",
                                reason="tree_counter_missing" if process_count is None else None),
        "rss_bytes": metric(rss, "bytes", "psutil_owned_workdir_filter",
                            reason="tree_counter_missing" if rss is None else None),
        "fd_count": metric(fds, "count", "psutil_owned_workdir_filter",
                          reason="tree_counter_missing" if fds is None else None),
    }


def _find_all_numeric(value: Any, names: set[str]) -> list[float]:
    found: list[float] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in names and type(child) in (int, float) and child >= 0:
                found.append(float(child))
            found.extend(_find_all_numeric(child, names))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_all_numeric(child, names))
    return found


def _observer_runtime_totals(events: Sequence[Mapping[str, Any]], names: set[str]) -> list[float]:
    totals: list[float] = []
    for event in events:
        runtime = event.get("runtime")
        if not isinstance(runtime, Mapping):
            continue
        values = []
        for supervisor in runtime.values():
            found = []
            if isinstance(supervisor, Mapping):
                for name in names:
                    direct = supervisor.get(name)
                    direct_value = direct.get("value") if isinstance(direct, Mapping) else direct
                    if type(direct_value) in (int, float) and direct_value >= 0:
                        found.append(float(direct_value))
                        break
                if not found:
                    found = _find_all_numeric(supervisor.get("heartbeat"), names)
            if found:
                values.append(found[0])
        if values:
            totals.append(sum(values))
    return totals


def _measure_dual(packages: Sequence[Any], root: Path, *, single_profile: Mapping[str, Any],
                  single_candidate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator
        from app.runtime.engine_host.store import FixtureStore
    except Exception as exc:
        raise CapacityEvidenceError("persistent_orchestrator_unavailable", type(exc).__name__) from exc
    budget = FixtureUpdateOrchestrator.build_fixture_budget(
        packages[0], packages[1], single_profile_digest=single_profile["profile_digest"],
        single_candidate_raw_digest=single_candidate["raw_digest"])
    projection_paths = {
        "rss_bytes": "metrics.resources.rss_bytes.max",
        "process_count": "metrics.resources.process_count.max",
        "fd_count": "metrics.resources.fd_count.max",
        "ipc_connections": "metrics.resources.ipc_connections.max",
        "queue_depth": "metrics.resources.queue_depth.max",
        "inflight": "metrics.resources.inflight.max",
    }
    budget["host_limits"] = {
        name: max(1, math.ceil(float(single_profile["thresholds"][path]["value"]) * 2))
        for name, path in projection_paths.items()
    }
    budget["projection_method"] = (
        "single_frozen_max_times_exact_generation_count_not_v2_certification")
    budget["budget_digest"] = digest_json(
        {key: value for key, value in budget.items() if key != "budget_digest"})
    store = FixtureStore(root / "state" / "capacity.sqlite3")
    orchestrator = FixtureUpdateOrchestrator(
        store=store, workdir=root / "orchestrator", capacity_profile=dict(single_profile),
        capacity_candidate=dict(single_candidate), fixture_budget=budget)
    timeline: list[dict[str, Any]] = []
    observer_events: list[dict[str, Any]] = []
    stop_sampling = threading.Event()

    def sampler() -> None:
        while not stop_sampling.wait(0.01):
            timeline.append(_workdir_process_sample(root))

    def observer(event: Mapping[str, Any]) -> None:
        payload = {**dict(event), "observer_received_at_utc": utc_now(),
                   "observer_monotonic_ns": time.monotonic_ns()}
        observer_events.append(json.loads(json.dumps(redact_sensitive(payload), default=str)))
        timeline.append(_workdir_process_sample(root))

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    started_ns = time.monotonic_ns()
    try:
        run_parameters = inspect.signature(orchestrator.run).parameters
        kwargs = {"cycles": MIN_DUAL_CYCLES}
        if "observer" in run_parameters:
            kwargs["observer"] = observer
        result = orchestrator.run(packages[0], packages[1], **kwargs)
    finally:
        stop_sampling.set()
        thread.join(timeout=2)
        timeline.append(_workdir_process_sample(root))
        store.close()
    result = redact_sensitive(result)
    events = result.get("events", []) if isinstance(result, Mapping) else []
    v2_events = [event for event in events if event.get("step") in {"v2_task", "activate"}]
    rollback_events = [event for event in events if event.get("step") in {"rollback_task", "rollback"}]
    observed_cycles = (result.get("cycles") if isinstance(result, Mapping) else None)
    cycle_reports = observed_cycles if isinstance(observed_cycles, list) else []
    if type(observed_cycles) is not int:
        observed_cycles = len(cycle_reports) or min(len(v2_events), len(rollback_events))
    new_invoke_durations = [float(item["new_invoke_duration_ms"]) for item in cycle_reports
                            if type(item.get("new_invoke_duration_ms")) in (int, float)]
    old_drain_durations = [float(item["old_drain_duration_ms"]) for item in cycle_reports
                           if type(item.get("old_drain_duration_ms")) in (int, float)]
    task_failures = 0
    task_rejections = 0
    if isinstance(result, Mapping):
        for task in result.get("tasks", []):
            status = task.get("status") if isinstance(task, Mapping) else None
            if status in {"failed", "unknown"}:
                task_failures += 1
            if status in {"rejected", "queue_full", "inflight_full"}:
                task_rejections += 1
    resource_values = {
        name: [sample[name]["value"] for sample in timeline
               if isinstance(sample.get(name), Mapping)
               and type(sample[name].get("value")) in (int, float)]
        for name in ("rss_bytes", "fd_count", "process_count")
    }
    queue_values = _observer_runtime_totals(
        observer_events, {"queue", "queued", "queue_depth"})
    inflight_values = _observer_runtime_totals(
        observer_events, {"inflight", "in_flight", "running"})
    connection_values = _observer_runtime_totals(
        observer_events, {"connections", "connection_count", "ipc_connections"})
    rss_trend = linear_slope(resource_values["rss_bytes"])
    raw = {
        "schema": RAW_SCHEMA, "kind": "dual", "started_at_utc": utc_now(),
        "duration_ms": (time.monotonic_ns() - started_ns) / 1_000_000,
        "cycles": MIN_DUAL_CYCLES, "orchestrator_result": result,
        "fixture_admission_budget": budget,
        "observer_events": observer_events, "resource_timeline": timeline,
        "sample_counts": {"dual_cycles": observed_cycles}, "completed_at_utc": utc_now(),
    }
    summary = {
        "dual": {
            "cycles": observed_cycles, "duration_ms": raw["duration_ms"],
            "v2_events": len(v2_events), "rollback_events": len(rollback_events),
            "new_invoke_ms": {**summarize(new_invoke_durations), "unit": "milliseconds",
                              "source": "persistent_orchestrator_cycle_reports"},
            "old_drain_ms": {**summarize(old_drain_durations), "unit": "milliseconds",
                             "source": "persistent_orchestrator_cycle_reports"},
            "task_failure_count": task_failures,
            "task_rejection_count": task_rejections,
            "expected_reconcile_required_count": sum(
                1 for task in (result.get("tasks", []) if isinstance(result, Mapping) else [])
                if isinstance(task, Mapping) and task.get("status") == "reconcile_required"),
            "resources": {
                **{name: {**summarize(values), "unit": "bytes" if name == "rss_bytes" else "count",
                          "source": "psutil_descendants_filtered_by_owned_workdir"}
                   for name, values in resource_values.items()},
                "queue_depth": {**summarize(queue_values), "unit": "count",
                                "source": "orchestrator_observer"},
                "inflight": {**summarize(inflight_values), "unit": "count",
                             "source": "orchestrator_observer"},
                "ipc_connections": {**summarize(connection_values), "unit": "count",
                                    "source": "orchestrator_observer"},
                "rss_trend_bytes_per_sample": {
                    "value": rss_trend, "unit": "bytes/sample",
                    "source": "least_squares_of_raw_dual_resource_timeline",
                    "method": "ordinary_least_squares_by_sample_index",
                },
            },
            "final_residual_processes": get_path(timeline[-1], "process_count.value"),
        }
    }
    return redact_sensitive(raw), summary


def _observed_for_thresholds(summary: Mapping[str, Any],
                             kind: str) -> dict[str, tuple[float, str, float]]:
    if kind == "single":
        return {
            "metrics.startup.cold_ms.p95": (get_path(summary, "startup.cold_ms.p95"), "milliseconds", 5.0),
            "metrics.startup.warm_ms.p95": (get_path(summary, "startup.warm_ms.p95"), "milliseconds", 5.0),
            "metrics.invoke_read.combined_regular_ms.p95": (
                get_path(summary, "invoke_read.combined_regular_ms.p95"), "milliseconds", 2.0),
            "metrics.lifecycle.drain_ms.max": (get_path(summary, "lifecycle.drain_ms.max"), "milliseconds", 5.0),
            "metrics.lifecycle.stop_ms.max": (get_path(summary, "lifecycle.stop_ms.max"), "milliseconds", 5.0),
            "metrics.resources.rss_bytes.max": (get_path(summary, "resources.rss_bytes.max"), "bytes", 1024 * 1024),
            "metrics.resources.process_count.max": (get_path(summary, "resources.process_count.max"), "count", 0.0),
            "metrics.resources.fd_count.max": (get_path(summary, "resources.fd_count.max"), "count", 2.0),
            "metrics.resources.queue_depth.max": (get_path(summary, "resources.queue_depth.max"), "count", 1.0),
            "metrics.resources.inflight.max": (get_path(summary, "resources.inflight.max"), "count", 1.0),
            "metrics.resources.ipc_connections.max": (get_path(summary, "resources.ipc_connections.max"), "count", 1.0),
            "metrics.lifecycle.max_residual_count": (get_path(summary, "lifecycle.max_residual_count"), "count", 0.0),
            "metrics.lifecycle.rss_trend_bytes_per_round.value": (
                get_path(summary, "lifecycle.rss_trend_bytes_per_round.value"), "bytes/round", 256 * 1024),
            "metrics.invoke_read.cases.over_limit.rejected": (
                get_path(summary, "invoke_read.cases.over_limit.rejected"), "count", 0.0),
        }
    return {
        "metrics.dual.duration_ms": (get_path(summary, "dual.duration_ms"), "milliseconds", 50.0),
        "metrics.dual.new_invoke_ms.p95": (get_path(summary, "dual.new_invoke_ms.p95"), "milliseconds", 5.0),
        "metrics.dual.old_drain_ms.max": (get_path(summary, "dual.old_drain_ms.max"), "milliseconds", 5.0),
        "metrics.dual.resources.rss_bytes.max": (get_path(summary, "dual.resources.rss_bytes.max"), "bytes", 1024 * 1024),
        "metrics.dual.resources.fd_count.max": (get_path(summary, "dual.resources.fd_count.max"), "count", 2.0),
        "metrics.dual.resources.process_count.max": (get_path(summary, "dual.resources.process_count.max"), "count", 0.0),
        "metrics.dual.resources.queue_depth.max": (get_path(summary, "dual.resources.queue_depth.max"), "count", 1.0),
        "metrics.dual.resources.inflight.max": (get_path(summary, "dual.resources.inflight.max"), "count", 1.0),
        "metrics.dual.resources.ipc_connections.max": (get_path(summary, "dual.resources.ipc_connections.max"), "count", 1.0),
        "metrics.dual.resources.rss_trend_bytes_per_sample.value": (
            get_path(summary, "dual.resources.rss_trend_bytes_per_sample.value"), "bytes/sample", 256 * 1024),
        "metrics.dual.final_residual_processes": (get_path(summary, "dual.final_residual_processes"), "count", 0.0),
        "metrics.dual.task_failure_count": (get_path(summary, "dual.task_failure_count"), "count", 0.0),
        "metrics.dual.task_rejection_count": (get_path(summary, "dual.task_rejection_count"), "count", 0.0),
    }


def _thresholds(summary: Mapping[str, Any], kind: str) -> dict[str, Any]:
    thresholds: dict[str, Any] = {}
    for path, (observed, unit, minimum_margin) in _observed_for_thresholds(summary, kind).items():
        if type(observed) not in (int, float):
            raise CapacityEvidenceError("calibration_required_metric_missing", path)
        if path.endswith("over_limit.rejected"):
            if float(observed) < 1:
                raise CapacityEvidenceError("calibration_overlimit_rejection_missing", path)
            thresholds[path] = {"comparison": "min", "value": 1, "unit": unit,
                                "calibration_observed": observed, "allowed_margin": 0,
                                "margin_policy": "explicit_minimum_one_rejection"}
        elif (path.endswith("max_residual_count") or path.endswith("final_residual_processes")
              or path.endswith("task_failure_count") or path.endswith("task_rejection_count")):
            if float(observed) != 0:
                raise CapacityEvidenceError("calibration_zero_budget_violated", path)
            thresholds[path] = {"comparison": "equal", "value": 0, "unit": unit,
                                "calibration_observed": observed, "allowed_margin": 0,
                                "margin_policy": "no_residual_allowed"}
        else:
            margin = max(float(minimum_margin), abs(float(observed)) * 0.20)
            baseline = max(0.0, float(observed)) if "rss_trend" in path else float(observed)
            thresholds[path] = {"comparison": "max", "value": baseline + margin,
                                "unit": unit, "calibration_observed": observed,
                                "allowed_margin": margin,
                                "margin_policy": "frozen_max_of_absolute_floor_or_20_percent"}
    return thresholds


def _report(kind: str, phase: str, context: Mapping[str, Any],
            packages: Sequence[Mapping[str, Any]], load: Mapping[str, Any],
            raw: Mapping[str, Any], summary: Mapping[str, Any], *,
            profile_digest: str | None,
            failures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA, "kind": kind, "phase": phase, "fixture_only": True,
        "qa_ops_acceptance": "pending", "generated_at_utc": utc_now(),
        "context": context, "packages": list(packages), "load": load,
        "profile_digest": profile_digest, "raw_digest": digest_json(raw),
        "raw": raw, "metrics": summary, "failures": list(failures),
        "status": ("candidate" if phase == "formal" and not failures else
                   "calibration" if phase == "calibration" and not failures else "failed"),
        "ok": not failures,
    }


def _failure_report(args: argparse.Namespace, code: str, detail: str = "") -> dict[str, Any]:
    return {"schema": REPORT_SCHEMA, "kind": args.kind, "phase": args.phase,
            "fixture_only": True, "qa_ops_acceptance": "pending", "ok": False,
            "status": "preflight_failed", "provider_started": False,
            "generated_at_utc": utc_now(), "failures": [{"code": code, "detail": detail}]}


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-profile", required=True)
    parser.add_argument("--phase", choices=("calibration", "formal"), required=True)
    parser.add_argument("--kind", choices=("single", "dual"), default="single")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--single-profile", help="frozen single profile required by dual")
    parser.add_argument("--single-candidate", help="formal single candidate required by dual")
    parser.add_argument("--samples", type=int, default=MIN_COLD)
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = _parse(argv)
    report_path = Path(args.report).resolve()
    workdir = Path(args.workdir).resolve()
    try:
        if args.samples < max(MIN_COLD, MIN_WARM):
            raise CapacityEvidenceError("sample_count_invalid", "cold_and_warm_require_at_least_30")
        if not Path(args.workdir).is_absolute():
            raise CapacityEvidenceError("workdir_must_be_absolute")
        if workdir.exists():
            raise CapacityEvidenceError("workdir_must_be_new")
        load = _load(args.samples, args.kind)
        profile = None
        single_profile_digest = None
        single_profile = None
        single_candidate = None

        if args.phase == "formal":
            if not args.profile:
                raise CapacityEvidenceError("profile_required")
            profile = validate_frozen_profile(_json_read(Path(args.profile)))
            _verify_calibration_evidence(profile)
            context = _context()
            if args.kind == "dual":
                if not args.single_profile:
                    raise CapacityEvidenceError("single_profile_required")
                if not args.single_candidate:
                    raise CapacityEvidenceError("single_candidate_required")
                single_profile = validate_frozen_profile(_json_read(Path(args.single_profile)))
                _verify_calibration_evidence(single_profile)
                if single_profile["kind"] != "single":
                    raise CapacityEvidenceError("single_profile_kind_invalid")
                single_profile_digest = single_profile["profile_digest"]
                if str(Path(args.single_profile).resolve()) != profile.get("single_profile_path"):
                    raise CapacityEvidenceError("single_profile_path_mismatch")
                if str(Path(args.single_candidate).resolve()) != profile.get("single_candidate_path"):
                    raise CapacityEvidenceError("single_candidate_path_mismatch")
                single_candidate = _validate_single_candidate(
                    single_profile, _json_read(Path(args.single_candidate)))
                if single_candidate["raw_digest"] != profile.get("single_candidate_raw_digest"):
                    raise CapacityEvidenceError("single_candidate_raw_digest_mismatch")
            packages = _load_packages(profile["packages"])
            identities = [
                _package_identity(package,
                                  artifact_path=Path(profile["packages"][index]["artifact_path"]))
                for index, package in enumerate(packages)
            ]
            formal_preflight(profile, kind=args.kind, context=context, packages=identities,
                             load=load, single_profile_digest=single_profile_digest)
        else:
            if args.profile:
                raise CapacityEvidenceError("calibration_profile_forbidden")
            context = _context()
            single_profile = None
            if args.kind == "dual":
                if not args.single_profile:
                    raise CapacityEvidenceError("single_profile_required")
                if not args.single_candidate:
                    raise CapacityEvidenceError("single_candidate_required")
                single_profile = validate_frozen_profile(_json_read(Path(args.single_profile)))
                _verify_calibration_evidence(single_profile)
                if single_profile["kind"] != "single":
                    raise CapacityEvidenceError("single_profile_kind_invalid")
                single_profile_digest = single_profile["profile_digest"]
                single_candidate = _validate_single_candidate(
                    single_profile, _json_read(Path(args.single_candidate)))
            if args.kind == "single":
                # Calibration mutation starts here. Formal checks above are complete.
                workdir.mkdir(parents=True, mode=0o700)
                packages, identities = _save_packages(
                    workdir=workdir, report=report_path, versions=("1.0.0",))
            else:
                assert single_profile is not None
                v1_packages = _load_packages(single_profile["packages"])
                v1_identities = [
                    _package_identity(package,
                                      artifact_path=Path(single_profile["packages"][index]["artifact_path"]))
                    for index, package in enumerate(v1_packages)
                ]
                formal_preflight(single_profile, kind="single", context=context,
                                 packages=v1_identities, load=single_profile["load"])
                workdir.mkdir(parents=True, mode=0o700)
                artifacts = report_path.parent / "capacity-artifacts"
                artifacts.mkdir(parents=True, exist_ok=True)
                v2, v2_identity = _build_saved_package(
                    api=_package_api(), build_root=artifacts / "payloads", artifacts=artifacts,
                    version="1.0.1", index=2)
                packages = [v1_packages[0], v2]
                identities = [v1_identities[0], v2_identity]

        if args.phase == "formal":
            workdir.mkdir(parents=True, mode=0o700)
        if args.kind == "single":
            raw, summary = _measure_single(packages[0], workdir / "measurements",
                                           samples=args.samples)
        else:
            assert single_profile is not None and single_candidate is not None
            raw, summary = _measure_dual(
                packages, workdir / "dual-measurements", single_profile=single_profile,
                single_candidate=single_candidate)

        failures: list[dict[str, Any]] = []
        if args.phase == "calibration":
            if args.kind == "single":
                if raw["sample_counts"]["invoke_read"] < MIN_INVOKE:
                    failures.append({"code": "invoke_samples_insufficient"})
                if not summary["isolation"]["healthy_uninterrupted"]:
                    failures.append({"code": "worker_isolation_failed"})
            expected_counts = _requirements(args.samples, args.kind)
            for name, required in expected_counts.items():
                observed = raw.get("sample_counts", {}).get(name)
                if type(observed) is not int or observed < required:
                    failures.append({"code": "raw_sample_count_insufficient", "sample": name,
                                     "observed": observed, "required": required})
            if failures:
                report = _report(args.kind, args.phase, context, identities, load, raw,
                                 summary, profile_digest=None, failures=failures)
                _json_write(report_path, report)
                return 3, report
            try:
                thresholds = _thresholds(summary, args.kind)
            except CapacityEvidenceError as exc:
                failures.append({"code": exc.code, "detail": exc.detail})
                report = _report(args.kind, args.phase, context, identities, load, raw,
                                 summary, profile_digest=None, failures=failures)
                _json_write(report_path, report)
                return 3, report
            raw_path = report_path.parent / f"{args.fixture_profile}-{args.kind}.calibration.raw.json"
            _json_write(raw_path, raw)
            profile_value = freeze_profile({
                "profile_id": args.fixture_profile, "kind": args.kind,
                "frozen_at_utc": utc_now(), "context": context, "packages": identities,
                "load": load, "sample_requirements": _requirements(args.samples, args.kind),
                "statistics": STATISTICS, "thresholds": thresholds,
                "required_metrics": list(thresholds),
                "calibration_raw_path": str(raw_path),
                "calibration_raw_digest": digest_json(raw),
                "single_profile_digest": single_profile_digest,
                "single_profile_path": (str(Path(args.single_profile).resolve())
                                        if args.kind == "dual" else None),
                "single_candidate_path": (str(Path(args.single_candidate).resolve())
                                          if args.kind == "dual" else None),
                "single_candidate_raw_digest": (
                    single_candidate["raw_digest"] if args.kind == "dual" else None),
            })
            assert_raw_counts(raw, profile_value)
            profile_path = report_path.parent / f"{args.fixture_profile}-{args.kind}.profile.json"
            _json_write(profile_path, profile_value)
            report = _report(args.kind, args.phase, context, identities, load, raw, summary,
                             profile_digest=profile_value["profile_digest"], failures=[])
            report["frozen_profile"] = str(profile_path)
            _json_write(report_path, report)
            return 0, report

        assert profile is not None
        try:
            assert_raw_counts(raw, profile)
        except CapacityEvidenceError as exc:
            failures.append({"code": exc.code, "detail": exc.detail})
        provisional = _report(args.kind, args.phase, context, identities, load, raw, summary,
                              profile_digest=profile["profile_digest"], failures=failures)
        failures.extend(evaluate_report(provisional, profile))
        report = _report(args.kind, args.phase, context, identities, load, raw, summary,
                         profile_digest=profile["profile_digest"], failures=failures)
        _json_write(report_path, report)
        return (0 if not failures else 3), report
    except CapacityEvidenceError as exc:
        report = _failure_report(args, exc.code, exc.detail)
        _json_write(report_path, report)
        return 2, report
    except Exception as exc:
        code = getattr(exc, "code", "measurement_failed")
        detail = getattr(exc, "detail", "") or type(exc).__name__
        report = _failure_report(args, code, detail)
        report["status"] = "measurement_failed"
        report["provider_started"] = None
        _json_write(report_path, report)
        return 4, report


def main(argv: Sequence[str] | None = None) -> int:
    code, report = run(argv)
    stream = sys.stdout if code == 0 else sys.stderr
    print(json.dumps({"ok": report["ok"], "status": report["status"],
                      "failures": report.get("failures", [])}, ensure_ascii=False), file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
