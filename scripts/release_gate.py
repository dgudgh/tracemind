"""Release gate: a verdict is only as good as the evidence files it is given.

Four quality bottom lines are checked separately because they fail separately:

    static          scripts/check_all.py --fast (links, contracts, static checks)
    local-http      real HTTP against an isolated backend (no fixture shortcuts)
    real-engines    the four engines + Oracle profile, collected on real instances
    soak            168h continuous run with latency and failure accounting

Absent evidence is reported as ``not_executed``, never as passed. That is the whole point:
a green gate must mean someone actually ran the thing, not that nobody wrote a failure down.

Usage:
    python scripts/release_gate.py                      # read reports/release-gate/
    python scripts/release_gate.py --evidence DIR
    python scripts/release_gate.py --expect-hours 24    # a shorter requirement than 168h
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "reports" / "release-gate"
TIERS = ("static", "local-http", "real-engines", "soak")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"tier": path.stem, "status": "invalid", "detail": f"unreadable: {type(exc).__name__}"}
    return value if isinstance(value, dict) else {"tier": path.stem, "status": "invalid", "detail": "not an object"}


def _verdict(name: str, value: dict, *, expect_hours: float | None) -> dict:
    status = value.get("status")
    reasons = []
    if status not in {"passed", "failed", "blocked"}:
        return {"tier": name, "status": "not_executed", "reason": f"no usable evidence ({value.get('detail') or status or 'missing'})"}
    if not isinstance(value.get("finished_at"), str) or not value["finished_at"]:
        reasons.append("missing finished_at")
    if name == "soak":
        hours = value.get("duration_hours")
        if not isinstance(hours, (int, float)) or hours <= 0:
            reasons.append("missing duration_hours")
        elif expect_hours is not None and hours + 0.01 < expect_hours:
            reasons.append(f"soak ran {hours}h, below the required {expect_hours}h")
        if value.get("failures", 0) != 0:
            reasons.append(f"{value.get('failures')} failed probes")
    if status == "failed":
        reasons.append("the tier reported failure")
    if status == "blocked":
        reasons.append(value.get("reason") or "the tier is blocked on an external dependency")
    return {"tier": name, "status": "failed" if reasons else "passed",
            "reason": "; ".join(reasons) or None,
            "finished_at": value.get("finished_at"),
            "detail": value.get("detail")}


def evaluate(evidence_dir: Path, *, expect_hours: float | None = 168.0) -> dict:
    rows = [_verdict(tier, _read(evidence_dir / f"{tier}.json"), expect_hours=expect_hours) for tier in TIERS]
    ok = all(row["status"] == "passed" for row in rows)
    return {"schema": "release.gate/v1", "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evidence_dir": str(evidence_dir), "tiers": rows, "verdict": "passed" if ok else "blocked"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate release evidence; never invents a pass.")
    parser.add_argument("--evidence", default=str(DEFAULT_DIR), help="directory holding <tier>.json files")
    # 168h is the published requirement, so it is the default: a short soak cannot pass by
    # simply omitting the flag.
    parser.add_argument("--expect-hours", type=float, default=168.0, help="minimum soak duration (default 168)")
    parser.add_argument("--json", action="store_true", help="print the machine-readable report only")
    args = parser.parse_args(argv)

    report = evaluate(Path(args.evidence), expect_hours=args.expect_hours)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"release gate — {report['verdict']}  ({report['evidence_dir']})")
        for row in report["tiers"]:
            detail = f" — {row['reason']}" if row.get("reason") else ""
            print(f"  {row['tier']:<13} {row['status']:<13}{detail}")
        missing = [row["tier"] for row in report["tiers"] if row["status"] == "not_executed"]
        if missing:
            print(f"\nnot executed: {', '.join(missing)}")
            print("Provide <tier>.json in the evidence directory after actually running it.")
    return 0 if report["verdict"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
