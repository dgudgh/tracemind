from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_rejects_relative_workdir_before_creating_files(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_fake_provider_runtime.py"),
         "--workdir", "relative-host-dir", "--report", str(tmp_path / "report.json"),
         "--fixture-calibration"], cwd=tmp_path, capture_output=True, text=True,
        timeout=10,
    )
    assert completed.returncode != 0
    assert "absolute private directory" in completed.stderr
    assert not (tmp_path / "relative-host-dir").exists()
    assert not (tmp_path / "report.json").exists()


def test_eight_real_host_sigkill_points_recover_from_new_process(tmp_path):
    """No mocked kill: the runner sends SIGKILL and starts a new Python Host."""
    report = tmp_path / "report.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_fake_provider_runtime.py"),
         "--workdir", str(tmp_path / "work"), "--report", str(report),
         "--cycles", "20", "--kill-case", "all", "--fixture-calibration"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    value = json.loads(report.read_text())
    assert value["ok"] is True
    assert len(value["normal"]["cycles"]) == 20
    assert {item["kill_point"] for item in value["kill_cases"]} == {
        "stage_complete", "provider_started_before_handshake", "selftest_before_cas",
        "cas_after_commit", "dual_generation_inflight", "effect_after_commit_before_receipt",
        "drain_midway", "rollback_after_cas",
    }
    assert all(item["host_exit"] == -9 and item["recover_exit"] == 0
               for item in value["kill_cases"])
    assert not value["cleanup"]["socket_files_after"]
    assert not value["cleanup"]["ipc_roots_after"]
    assert not value["cleanup"]["runtime_snapshots_after"]
    for case in value["kill_cases"]:
        for artifact in case["report"]["launch_artifacts"]:
            if artifact["state"] == "cleaned":
                assert not Path(artifact["path"]).exists()
            elif artifact["state"] == "retained_audit":
                assert artifact["retained_reason"]

    def check_public_evidence(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"owner_token", "launch_secret", "private_key", "secret"}:
                    assert isinstance(child, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", child)
                check_public_evidence(child)
        elif isinstance(item, list):
            for child in item:
                check_public_evidence(child)
        elif isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(item)
            except ValueError:
                return
            check_public_evidence(decoded)

    check_public_evidence(value)
