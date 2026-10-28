#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from unittest.mock import patch

from _bootstrap import add_backend_to_path

os.environ.setdefault("DEBUG", "false")
add_backend_to_path()

from app.models.db_instance import DBInstance
from app.services import capacity_monitor, perf_baseline


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def instance(db_type: str) -> DBInstance:
    return DBInstance(
        id={"postgresql": 901, "sqlserver": 902}.get(db_type, 900),
        instance_name=f"dispatch-{db_type}",
        db_type=db_type,
        host="127.0.0.1",
        port=5432 if db_type == "postgresql" else 1433,
        username="tester",
        password_cipher="",
        enabled=1,
    )


def fail_mysql(*_args, **_kwargs):
    raise AssertionError("MySQL collector must not be called for non-MySQL instances")


def main() -> int:
    results = []
    with patch.object(perf_baseline, "_collect_mysql", side_effect=fail_mysql), patch.object(
        capacity_monitor, "_mysql_capacity_rows", side_effect=fail_mysql
    ):
        for db_type in ("postgresql", "sqlserver"):
            inst = instance(db_type)
            perf = perf_baseline.collect_perf_metrics(FakeSession(), inst)
            capacity = capacity_monitor.collect_current_capacity(inst)
            results.append(
                {
                    "db_type": db_type,
                    "perf": perf,
                    "capacity": {
                        "object_count": len(capacity.get("objects") or []),
                        "warnings": capacity.get("warnings") or [],
                    },
                    "pass": perf.get("count") == 0 and bool(perf.get("warnings")) and bool(capacity.get("warnings")),
                }
            )
    report = {
        "ok": all(item["pass"] for item in results),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
