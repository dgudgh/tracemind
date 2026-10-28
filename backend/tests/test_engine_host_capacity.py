from __future__ import annotations

import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from app.runtime.engine_host.metrics import (
    REQUIRED_METRICS_BY_KIND,
    CapacityEvidenceError,
    assert_raw_counts,
    evaluate_report,
    formal_preflight,
    freeze_profile,
    redact_sensitive,
    sample_process_tree,
    summarize,
    validate_frozen_profile,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts/measure_engine_capacity.py"
SPEC = importlib.util.spec_from_file_location("measure_engine_capacity", SCRIPT_PATH)
assert SPEC and SPEC.loader
capacity_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity_script)


def _load_fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures/engine_capacity" / name
    return json.loads(path.read_text())


def _bindings(kind: str = "single") -> tuple[dict, list[dict], dict]:
    context = {
        "machine": {
            "os": {"system": "Darwin", "release": "25.0", "version": "fixture"},
            "cpu": {"machine": "arm64", "processor": "arm"},
            "python": {"implementation": "CPython", "version": "3.13.0",
                       "abi": "cpython-313", "executable_sha256": "a" * 64},
        },
        "engine_runtime": {"host_digest": "b" * 64, "sdk_digest": "c" * 64,
                           "launcher_digest": "d" * 64},
        "measurement_tool_sha256": "e" * 64,
    }
    packages = [{
        "version": "1.0.0", "package_digest": "1" * 64,
        "source_digest": "0" * 64, "manifest_sha256": "2" * 64, "payload_digest": "3" * 64,
        "runtime_digest": "4" * 64, "artifact_path": "/fixture/v1.json",
        "artifact_sha256": "5" * 64,
    }]
    load = _load_fixture("single-load-profile.json")
    if kind == "dual":
        packages.append({**packages[0], "version": "1.0.1",
                         "package_digest": "6" * 64, "payload_digest": "7" * 64,
                         "artifact_path": "/fixture/v2.json", "artifact_sha256": "8" * 64})
        load = _load_fixture("dual-load-profile.json")
    return context, packages, load


def _profile(kind: str = "single") -> dict:
    context, packages, load = _bindings(kind)
    if kind == "single":
        requirements = {"cold_starts": 30, "warm_starts": 30,
                        "invoke_read": 200, "lifecycle_rounds": 20}
        thresholds = {name: {"comparison": "max", "value": 100.0, "unit": "fixture",
                             "calibration_observed": 90.0, "allowed_margin": 10.0}
                      for name in REQUIRED_METRICS_BY_KIND[kind]}
    else:
        requirements = {"dual_cycles": 20}
        thresholds = {name: {"comparison": "max", "value": 100000000.0, "unit": "fixture",
                             "calibration_observed": 90000000.0, "allowed_margin": 10000000.0}
                      for name in REQUIRED_METRICS_BY_KIND[kind]}
    return freeze_profile({
        "profile_id": f"fixture-{kind}", "kind": kind, "frozen_at_utc": "2026-10-26T00:00:00Z",
        "context": context, "packages": packages, "load": load,
        "sample_requirements": requirements,
        "statistics": {"p95": "nearest-rank-ceil"},
        "thresholds": thresholds, "required_metrics": list(thresholds),
        "calibration_raw_path": "/fixture/calibration.raw.json",
        "calibration_raw_digest": "9" * 64,
        "single_profile_digest": "a" * 64 if kind == "dual" else None,
        "single_profile_path": "/fixture/single.profile.json" if kind == "dual" else None,
        "single_candidate_path": "/fixture/single.candidate.json" if kind == "dual" else None,
        "single_candidate_raw_digest": "b" * 64 if kind == "dual" else None,
    })


def test_fixture_load_profiles_freeze_exact_required_load():
    single = _load_fixture("single-load-profile.json")
    dual = _load_fixture("dual-load-profile.json")
    assert single == capacity_script._load(30, "single")
    assert dual == capacity_script._load(30, "dual")
    assert sum(case["calls"] for case in single["invoke_read"]["cases"]
               if case["name"] != "over_limit") >= 200
    assert {case["concurrency"] for case in single["invoke_read"]["cases"]} >= {1, 4, 8}
    assert dual["dual_cycles"] == 20


def test_saved_calibration_package_reuses_public_trust_without_private_key(tmp_path):
    packages, identities = capacity_script._save_packages(
        workdir=tmp_path / "build", report=tmp_path / "report.json", versions=("1.0.0",))
    artifact = Path(identities[0]["artifact_path"])
    value = json.loads(artifact.read_text())
    assert value["digest"] == identities[0]["package_digest"]
    assert packages[0].trusted_keys
    serialized = artifact.read_text().lower()
    assert "private_key" not in serialized
    assert "private key" not in serialized
    assert all(set(key) == {"raw", "namespace", "channel", "publisher", "engine_scopes"}
               for key in value["trusted_keys"])


def test_real_process_sample_preserves_source_units_and_identity():
    sample = sample_process_tree(os.getpid())
    assert sample.identity_match is None
    assert sample.process_count["unit"] == "count"
    assert sample.rss_bytes["unit"] == "bytes"
    assert sample.process_count["source"] in {"psutil", "not_measured"}
    if sample.rss_bytes["value"] is None:
        assert sample.rss_bytes["reason"]
    else:
        assert sample.rss_bytes["value"] > 0


def test_saved_evidence_hashes_ownership_tokens_instead_of_persisting_them():
    value = redact_sensitive({"owner_token": "do-not-save", "nested": [{"secret": "x"}],
                              "launch_instance_id": "safe-id"})
    serialized = json.dumps(value)
    assert "do-not-save" not in serialized and '"x"' not in serialized
    assert value["owner_token"].startswith("sha256:")
    assert value["launch_instance_id"] == "safe-id"
    assert redact_sensitive(value) == value


def test_json_write_redacts_nested_runtime_secrets_before_persisting(tmp_path):
    path = tmp_path / "evidence.json"
    capacity_script._json_write(path, {
        "record": {"owner_token": "owner-plaintext"},
        "events": [{"runtime": {"launch_secret": "launch-plaintext"}}],
        "trusted_keys": [{"raw": "public-key-is-evidence"}],
    })
    serialized = path.read_text()
    value = json.loads(serialized)
    assert "owner-plaintext" not in serialized
    assert "launch-plaintext" not in serialized
    assert value["record"]["owner_token"].startswith("sha256:")
    assert value["events"][0]["runtime"]["launch_secret"].startswith("sha256:")
    assert value["trusted_keys"][0]["raw"] == "public-key-is-evidence"


def test_dual_observer_sums_concurrent_supervisors_for_peak_counts():
    heartbeat_events = [{"runtime": {
        "old": {"heartbeat": {"queue": 1, "inflight": 2, "connections": 1}},
        "candidate": {"heartbeat": {"queue": 3, "inflight": 4, "connections": 1}},
    }}]
    assert capacity_script._observer_runtime_totals(heartbeat_events, {"queue"}) == [4.0]
    assert capacity_script._observer_runtime_totals(heartbeat_events, {"inflight"}) == [6.0]
    assert capacity_script._observer_runtime_totals(heartbeat_events, {"connections"}) == [2.0]
    metric_events = [{"runtime": {
        "old": {"queue_depth": {"value": 1}, "inflight": {"value": 2},
                "ipc_connections": {"value": 1}},
        "candidate": {"queue_depth": {"value": 3}, "inflight": {"value": 4},
                      "ipc_connections": {"value": 1}},
    }}]
    assert capacity_script._observer_runtime_totals(metric_events, {"queue_depth"}) == [4.0]
    assert capacity_script._observer_runtime_totals(metric_events, {"inflight"}) == [6.0]
    assert capacity_script._observer_runtime_totals(metric_events, {"ipc_connections"}) == [2.0]


def test_raw_sample_statistics_are_recomputed_from_values():
    assert summarize([7, 1, 5, 3]) == {"count": 4, "median": 4.0, "p95": 7.0, "max": 7.0}
    raw = {
        "cold_starts": [{"start": {"duration_ms": value}, "error": None}
                        for value in (10, 20, 30)],
        "warm_starts": [{"start": {"duration_ms": value}, "error": None}
                        for value in (5, 6, 7)],
        "invoke": {"successful_regular_calls": 3, "cases": [{
            "name": "concurrency_1", "samples": [
                {"ok": True, "duration_ms": 1}, {"ok": True, "duration_ms": 2},
                {"ok": True, "duration_ms": 9}],
            "successful": 3, "rejected": 0, "failed": 0, "resource_timeline": [],
        }, {"name": "over_limit", "expect_rejection": True, "samples": [],
            "successful": 0, "rejected": 2, "failed": 0, "resource_timeline": []}]},
        "lifecycle": [{"drain": {"duration_ms": 3}, "stop": {"duration_ms": 4},
                       "after_stop": {"residual_count": 0}, "resource_timeline": []}],
        "isolation": {"healthy_uninterrupted": True},
    }
    summary = capacity_script._single_summary(raw)
    assert {key: summary["startup"]["cold_ms"][key]
            for key in ("count", "median", "p95", "max")} == {
        "count": 3, "median": 20.0, "p95": 30.0, "max": 30.0}
    assert summary["startup"]["cold_ms"]["unit"] == "milliseconds"
    assert summary["invoke_read"]["combined_regular_ms"]["p95"] == 9.0


def test_profile_digest_detects_any_post_freeze_edit():
    profile = _profile()
    validate_frozen_profile(profile)
    profile["context"]["machine"]["os"]["system"] = "Linux"
    with pytest.raises(CapacityEvidenceError, match="profile_digest_mismatch"):
        validate_frozen_profile(profile)


@pytest.mark.parametrize(
    ("area", "mutate", "code"),
    [
        ("context", lambda value: value["machine"]["os"].update(system="Linux"),
         "profile_context_mismatch"),
        ("context", lambda value: value["machine"]["cpu"].update(machine="x86_64"),
         "profile_context_mismatch"),
        ("context", lambda value: value["machine"]["python"].update(abi="cpython-312"),
         "profile_context_mismatch"),
        ("context", lambda value: value["engine_runtime"].update(host_digest="f" * 64),
         "profile_context_mismatch"),
        ("context", lambda value: value["engine_runtime"].update(sdk_digest="f" * 64),
         "profile_context_mismatch"),
        ("context", lambda value: value["engine_runtime"].update(launcher_digest="f" * 64),
         "profile_context_mismatch"),
        ("packages", lambda value: value[0].update(package_digest="f" * 64),
         "profile_packages_mismatch"),
        ("packages", lambda value: value[0].update(source_digest="f" * 64),
         "profile_packages_mismatch"),
        ("packages", lambda value: value[0].update(payload_digest="f" * 64),
         "profile_packages_mismatch"),
        ("packages", lambda value: value[0].update(runtime_digest="f" * 64),
         "profile_packages_mismatch"),
        ("load", lambda value: value.update(sample_interval_ms=11),
         "profile_load_mismatch"),
    ],
    ids=["os", "cpu", "python-abi", "host-digest", "sdk-digest", "launcher-digest",
         "package-digest", "source-digest", "payload-digest", "runtime-digest", "load"],
)
def test_formal_preflight_exactly_binds_every_context(area, mutate, code):
    profile = _profile()
    context, packages, load = _bindings()
    target = {"context": context, "packages": packages, "load": load}[area]
    mutate(target)
    with pytest.raises(CapacityEvidenceError) as error:
        formal_preflight(profile, kind="single", context=context, packages=packages, load=load)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("field", "value"),
    [("cold_starts", 1), ("warm_starts", 0), ("invoke_read", -1), ("lifecycle_rounds", 19)],
)
def test_profile_rejects_insufficient_or_invalid_sample_counts(field, value):
    profile = _profile()
    profile["sample_requirements"][field] = value
    profile = freeze_profile(profile)
    with pytest.raises(CapacityEvidenceError) as error:
        validate_frozen_profile(profile)
    assert error.value.code == "profile_sample_requirement_invalid"


@pytest.mark.parametrize("bad_threshold", [
    {"comparison": "max", "value": -1, "unit": "ms", "allowed_margin": 0},
    {"comparison": "max", "value": 1, "unit": "ms"},
    {"comparison": "wider", "value": 1, "unit": "ms", "allowed_margin": 0},
], ids=["negative", "missing-margin", "invalid-comparison"])
def test_profile_rejects_illegal_thresholds(bad_threshold):
    profile = _profile()
    profile["thresholds"] = {"metrics.x": bad_threshold}
    profile["required_metrics"] = ["metrics.x"]
    profile = freeze_profile(profile)
    with pytest.raises(CapacityEvidenceError) as error:
        validate_frozen_profile(profile)
    assert error.value.code == "profile_threshold_invalid"


def test_profile_cannot_drop_a_mandatory_metric_or_threshold():
    profile = _profile()
    missing = "metrics.resources.inflight.max"
    profile["required_metrics"].remove(missing)
    profile["thresholds"].pop(missing)
    profile = freeze_profile(profile)
    with pytest.raises(CapacityEvidenceError) as error:
        validate_frozen_profile(profile)
    assert error.value.code == "profile_required_metrics_missing"


def test_dual_profile_rejects_less_than_twenty_real_cycles():
    profile = _profile("dual")
    profile["sample_requirements"]["dual_cycles"] = 1
    profile = freeze_profile(profile)
    with pytest.raises(CapacityEvidenceError) as error:
        validate_frozen_profile(profile)
    assert error.value.code == "profile_sample_requirement_invalid"


def test_budget_and_required_metric_failures_are_nonempty_and_exact():
    profile = _profile()
    missing = evaluate_report({"metrics": {}}, profile)
    assert {failure["code"] for failure in missing} == {
        "required_metric_missing", "threshold_metric_missing"}
    assert {failure["metric"] for failure in missing} == REQUIRED_METRICS_BY_KIND["single"]
    exceeded = evaluate_report({"metrics": {"startup": {"cold_ms": {"p95": 101}}}}, profile)
    budget = next(failure for failure in exceeded
                  if failure["code"] == "budget_exceeded"
                  and failure["metric"] == "metrics.startup.cold_ms.p95")
    assert budget["threshold"] == 100.0


def test_raw_sample_counts_must_match_frozen_profile():
    profile = _profile()
    raw = {"sample_counts": {"cold_starts": 30, "warm_starts": 30,
                             "invoke_read": 199, "lifecycle_rounds": 20}}
    with pytest.raises(CapacityEvidenceError) as error:
        assert_raw_counts(raw, profile)
    assert error.value.code == "raw_sample_count_insufficient"
    assert error.value.detail == "invoke_read"


def test_formal_calibration_raw_digest_is_checked_before_statistics(tmp_path):
    raw = {"schema": "engine.capacity-raw/v2", "kind": "single", "sample_counts": {
        "cold_starts": 30, "warm_starts": 30, "invoke_read": 200, "lifecycle_rounds": 20}}
    raw_path = tmp_path / "calibration.raw.json"
    raw_path.write_text(json.dumps(raw))
    profile = _profile()
    profile["calibration_raw_path"] = str(raw_path)
    profile["calibration_raw_digest"] = capacity_script.digest_json(raw)
    profile = freeze_profile(profile)
    raw["sample_counts"]["cold_starts"] = 1
    raw_path.write_text(json.dumps(raw))
    with pytest.raises(CapacityEvidenceError) as error:
        capacity_script._verify_calibration_evidence(profile)
    assert error.value.code == "calibration_raw_digest_mismatch"


def test_cli_rejects_one_sample_without_creating_workdir_or_starting(tmp_path, monkeypatch):
    workdir = tmp_path / "must-not-exist"
    report = tmp_path / "report.json"
    monkeypatch.setattr(capacity_script, "_new_supervisor",
                        lambda *_args, **_kwargs: pytest.fail("Supervisor must not start"))
    code, result = capacity_script.run([
        "--fixture-profile", "fixture", "--phase", "formal", "--samples", "1",
        "--workdir", str(workdir), "--report", str(report),
    ])
    assert code != 0 and result["ok"] is False
    assert result["provider_started"] is False
    assert result["failures"][0]["code"] == "sample_count_invalid"
    assert not workdir.exists()


def test_cli_missing_profile_fails_before_context_or_supervisor(tmp_path, monkeypatch):
    workdir = tmp_path / "must-not-exist"
    monkeypatch.setattr(capacity_script, "_context",
                        lambda: pytest.fail("context is after profile presence check"))
    monkeypatch.setattr(capacity_script, "_new_supervisor",
                        lambda *_args, **_kwargs: pytest.fail("Supervisor must not start"))
    code, result = capacity_script.run([
        "--fixture-profile", "fixture", "--phase", "formal",
        "--workdir", str(workdir), "--report", str(tmp_path / "report.json"),
    ])
    assert code != 0 and result["failures"][0]["code"] == "profile_required"
    assert not workdir.exists()


@pytest.mark.parametrize(("mismatch", "expected_code"), [
    ("os", "profile_context_mismatch"),
    ("source_digest", "profile_packages_mismatch"),
])
def test_cli_formal_binding_mismatch_never_constructs_supervisor(
        tmp_path, monkeypatch, mismatch, expected_code):
    profile = _profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    workdir = tmp_path / "must-not-exist"
    context, packages, _load = _bindings()
    if mismatch == "os":
        context["machine"]["os"]["system"] = "Linux"
    else:
        packages[0]["source_digest"] = "f" * 64
    monkeypatch.setattr(capacity_script, "_context", lambda: context)
    monkeypatch.setattr(capacity_script, "_verify_calibration_evidence", lambda _profile: {})
    monkeypatch.setattr(capacity_script, "_load_packages", lambda _identities: [object()])
    monkeypatch.setattr(capacity_script, "_package_identity",
                        lambda _package, artifact_path: packages[0])
    monkeypatch.setattr(capacity_script, "_new_supervisor",
                        lambda *_args, **_kwargs: pytest.fail("Supervisor must not start"))
    code, result = capacity_script.run([
        "--fixture-profile", "fixture", "--phase", "formal", "--profile", str(profile_path),
        "--workdir", str(workdir), "--report", str(tmp_path / "report.json"),
    ])
    assert code != 0 and result["failures"][0]["code"] == expected_code
    assert result["provider_started"] is False
    assert not workdir.exists()


def test_cli_tampered_profile_never_reads_runtime_or_starts(tmp_path, monkeypatch):
    profile = _profile()
    profile["load"]["sample_interval_ms"] = 999
    profile_path = tmp_path / "tampered.json"
    profile_path.write_text(json.dumps(profile))
    monkeypatch.setattr(capacity_script, "_context",
                        lambda: pytest.fail("digest validation must happen first"))
    monkeypatch.setattr(capacity_script, "_new_supervisor",
                        lambda *_args, **_kwargs: pytest.fail("Supervisor must not start"))
    workdir = tmp_path / "must-not-exist"
    code, result = capacity_script.run([
        "--fixture-profile", "fixture", "--phase", "formal", "--profile", str(profile_path),
        "--workdir", str(workdir), "--report", str(tmp_path / "report.json"),
    ])
    assert code != 0 and result["failures"][0]["code"] == "profile_digest_mismatch"
    assert not workdir.exists()


def test_dual_profile_requires_exact_single_profile_digest():
    profile = _profile("dual")
    context, packages, load = _bindings("dual")
    with pytest.raises(CapacityEvidenceError) as error:
        formal_preflight(profile, kind="dual", context=context, packages=packages, load=load,
                         single_profile_digest="b" * 64)
    assert error.value.code == "single_profile_digest_mismatch"


@pytest.mark.parametrize(("summary", "expected_code"), [
    ({"startup": {"cold_ms": {"p95": 101}}}, "budget_exceeded"),
    ({"startup": {"cold_ms": {"p95": None}}}, "required_metric_missing"),
], ids=["budget-exceeded", "required-metric-missing"])
def test_formal_budget_or_missing_metric_is_nonzero_and_preserves_raw(
        tmp_path, monkeypatch, summary, expected_code):
    profile = _profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    context, packages, _load = _bindings()
    raw = {"schema": "engine.capacity-raw/v2", "sample_counts": {
        "cold_starts": 30, "warm_starts": 30,
        "invoke_read": 200, "lifecycle_rounds": 20,
    }, "evidence": [{"duration_ms": 101, "source": "fixture", "unit": "milliseconds"}]}
    monkeypatch.setattr(capacity_script, "_context", lambda: context)
    monkeypatch.setattr(capacity_script, "_verify_calibration_evidence", lambda _profile: {})
    monkeypatch.setattr(capacity_script, "_load_packages", lambda _identities: [object()])
    monkeypatch.setattr(capacity_script, "_package_identity",
                        lambda _package, artifact_path: packages[0])
    monkeypatch.setattr(capacity_script, "_measure_single",
                        lambda *_args, **_kwargs: (raw, summary))
    workdir = tmp_path / "formal-work"
    code, result = capacity_script.run([
        "--fixture-profile", "fixture", "--phase", "formal", "--profile", str(profile_path),
        "--workdir", str(workdir), "--report", str(tmp_path / "report.json"),
    ])
    assert code != 0 and result["ok"] is False
    assert result["raw"] == raw
    assert expected_code in {failure["code"] for failure in result["failures"]}
