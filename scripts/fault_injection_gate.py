#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from _bootstrap import add_backend_to_path

add_backend_to_path()

from app.core.database import Base
from app.state_engine.fault_injection import SCENARIO_CATALOG, run_scenario


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic IO, memory, network, and service-crash fault replay gates.",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.rounds < 1 or args.rounds > 100:
        raise SystemExit("--rounds must be between 1 and 100")

    with TemporaryDirectory(prefix="tracemind-fault-gate-") as temp_dir:
        engine = create_engine(f"sqlite:///{temp_dir}/fault-gate.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        base = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
        results = []
        with Session() as db:
            for scenario_index, scenario_key in enumerate(SCENARIO_CATALOG):
                for round_no in range(1, args.rounds + 1):
                    run_id = f"{scenario_key}-round-{round_no}"
                    start = base + timedelta(hours=scenario_index, minutes=round_no * 10)
                    results.append(run_scenario(
                        db,
                        scenario_key,
                        run_id=run_id,
                        start=start,
                    ))
        engine.dispose()

    summary = {
        "scenario_count": len(SCENARIO_CATALOG),
        "rounds_per_scenario": args.rounds,
        "run_count": len(results),
        "passed_runs": sum(1 for item in results if item["passed"]),
        "failed_runs": sum(1 for item in results if not item["passed"]),
    }
    report = {
        "gate_id": "TM-ISE-110",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "deterministic_replay",
        "scope_note": (
            "This gate validates state recognition, evidence completeness, counter-evidence, "
            "pattern matching, and root-cause output. It does not replace a privileged real-host "
            "fio/leak/tc/kill injection run."
        ),
        "summary": summary,
        "passed": summary["failed_runs"] == 0,
        "results": results,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
