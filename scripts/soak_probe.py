"""Soak/Latency probe for the release gate.

Runs a fixed request mix against a real HTTP surface for a bounded duration and writes the
evidence the gate expects. It measures what an operator feels (status + wall latency) and
never retries: a failed probe is a recorded failure, not something to hide by trying again.

Usage:
    python scripts/soak_probe.py --base-url http://127.0.0.1:3300 --hours 168 \
        --out reports/release-gate/soak.json
    python scripts/soak_probe.py --base-url http://127.0.0.1:3300 --seconds 30   # smoke

`--path` may be repeated; endpoints should be unauthenticated health/read probes, or the
caller supplies one bearer token with `--token-env` (the value is read from the environment
so it never lands in argv or the evidence file).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATHS = ("/health",)


def _one(base: str, path: str, token: str | None, timeout: float) -> tuple[bool, float, str | None]:
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(65536)
            ok = 200 <= response.status < 400
            return ok, time.perf_counter() - started, None if ok else f"http_{response.status}"
    except urllib.error.HTTPError as exc:
        return False, time.perf_counter() - started, f"http_{exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, time.perf_counter() - started, type(exc).__name__


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 4)


def run(base_url: str, *, duration_seconds: float, interval: float, paths: list[str],
        timeout: float, token: str | None, tier: str = "soak") -> dict:
    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_seconds
    latencies: list[float] = []
    failures: list[dict] = []
    probes = 0
    while time.monotonic() < deadline:
        for path in paths:
            ok, elapsed, reason = _one(base_url, path, token, timeout)
            probes += 1
            latencies.append(elapsed)
            if not ok:
                failures.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "path": path, "reason": reason, "latency_seconds": round(elapsed, 4)})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    finished_at = datetime.now(timezone.utc)
    hours = round((finished_at - started_at).total_seconds() / 3600, 4)
    return {
        "schema": "release.evidence/v1",
        "tier": tier,
        "status": "passed" if probes and not failures else "failed",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_hours": hours,
        "base_url": base_url,
        "paths": paths,
        "interval_seconds": interval,
        "probes": probes,
        "failures": len(failures),
        "failure_samples": failures[:50],
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": round(max(latencies), 4) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 4) if latencies else 0.0,
        },
        "detail": f"{probes} probes over {hours}h; {len(failures)} failures",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded soak probe that writes gate evidence.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--path", action="append", default=None)
    parser.add_argument("--token-env", default=None, help="environment variable holding a bearer token")
    parser.add_argument("--out", default=None)
    parser.add_argument("--tier", default="soak", choices=["soak", "local-http", "authenticated-http"],
                        help="evidence tier this run belongs to (default soak)")
    args = parser.parse_args(argv)

    duration = args.seconds or args.hours * 3600
    if duration <= 0:
        parser.error("set --hours or --seconds")
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        parser.error(f"{args.token_env} is empty")

    report = run(args.base_url, duration_seconds=duration, interval=args.interval,
                 paths=args.path or list(DEFAULT_PATHS), timeout=args.timeout, token=token,
                 tier=args.tier)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
