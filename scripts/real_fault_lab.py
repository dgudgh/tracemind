#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import add_backend_to_path

add_backend_to_path()

from app.state_engine.real_fault_lab import (  # noqa: E402
    EXECUTION_ACK,
    LabConfig,
    SCENARIOS,
    run_lab,
    write_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run bounded isolated Linux fault workloads; defaults to a non-executing plan.",
    )
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, dest="scenarios")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge", help=f"required for --execute: {EXECUTION_ACK}")
    parser.add_argument("--duration-seconds", type=int, default=5)
    parser.add_argument("--memory-mb", type=int, default=128)
    parser.add_argument("--io-mb", type=int, default=64)
    parser.add_argument("--network-delay-ms", type=int, default=120)
    parser.add_argument("--network-loss-pct", type=float, default=2.0)
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/tracemind-fault-lab"))
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_lab(
        LabConfig(
            duration_seconds=args.duration_seconds,
            memory_mb=args.memory_mb,
            io_mb=args.io_mb,
            network_delay_ms=args.network_delay_ms,
            network_loss_pct=args.network_loss_pct,
            workspace=args.workspace,
        ),
        scenarios=args.scenarios or SCENARIOS,
        execute=args.execute,
        acknowledgement=args.acknowledge,
    )
    if args.output:
        write_report(args.output, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
