from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.inspection import policy_service  # noqa: E402


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_dt(actual, expected, label):
    assert_equal(actual.isoformat(sep=" ", timespec="minutes") if actual else None, expected, label)


def main():
    assert_equal(policy_service.format_schedule("weekdays", None, "09:30"), "weekdays:09:30", "format weekdays")
    weekdays = policy_service.parse_schedule("weekdays:09:30")
    assert_equal(weekdays["type"], "weekdays", "parse weekdays type")
    assert_equal(weekdays["days"], [1, 2, 3, 4, 5], "parse weekdays days")
    monday = SimpleNamespace(enabled=True, schedule="weekdays:09:30", report_config_json={})
    assert_equal(policy_service.is_policy_due(monday, datetime(2026, 7, 6, 9, 31)), True, "weekday due after time")

    weekly = policy_service.format_schedule("weekly", [1, 3, 5], "08:00")
    assert_equal(weekly, "weekly:1,3,5:08:00", "format weekly multi-day")
    assert_equal(policy_service.parse_schedule(weekly)["days"], [1, 3, 5], "parse weekly days")
    assert_dt(
        policy_service.compute_next_run_time(weekly, now=datetime(2026, 7, 7, 12, 0)),
        "2026-07-08 08:00",
        "next weekly multi-day",
    )
    ran_wednesday = SimpleNamespace(
        enabled=True,
        schedule=weekly,
        report_config_json={"last_run_at": "2026-07-08 08:01:00"},
    )
    assert_equal(policy_service.is_policy_due(ran_wednesday, datetime(2026, 7, 8, 8, 30)), False, "weekly no duplicate run")

    monthly = policy_service.format_schedule("monthly", [1, 15, 28], "01:15")
    assert_equal(monthly, "monthly:1,15,28:01:15", "format monthly multi-day")
    assert_equal(policy_service.parse_schedule(monthly)["days"], [1, 15, 28], "parse monthly days")
    assert_dt(
        policy_service.compute_next_run_time(monthly, now=datetime(2026, 7, 16, 10, 0)),
        "2026-07-28 01:15",
        "next monthly multi-day",
    )

    quarterly = policy_service.format_schedule("quarterly", 15, "03:30", month=2)
    assert_equal(quarterly, "quarterly:2:15:03:30", "format quarterly")
    parsed_quarterly = policy_service.parse_schedule(quarterly)
    assert_equal(parsed_quarterly["month"], 2, "parse quarterly month")
    assert_equal(parsed_quarterly["day"], 15, "parse quarterly day")
    assert_dt(
        policy_service.compute_next_run_time(quarterly, now=datetime(2026, 3, 1, 10, 0), last_run_at="2026-02-15 03:31:00"),
        "2026-05-15 03:30",
        "next quarterly after current quarter ran",
    )


if __name__ == "__main__":
    main()
