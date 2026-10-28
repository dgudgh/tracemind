from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def main() -> int:
    from app.core.database import SessionLocal
    from app.services.observability import binding_service

    config = {
        "host": os.getenv("MESGUARD_BINDING_TEST_HOST"),
        "zabbix_hostid": os.getenv("MESGUARD_BINDING_TEST_ZABBIX_HOST_ID"),
        "postgresql_instance_id": os.getenv("MESGUARD_BINDING_TEST_POSTGRESQL_INSTANCE_ID"),
        "mysql_instance_id": os.getenv("MESGUARD_BINDING_TEST_MYSQL_INSTANCE_ID"),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required binding test environment values: {', '.join(missing)}")
    host = str(config["host"])
    zabbix_hostid = str(config["zabbix_hostid"])
    postgresql_instance_id = int(str(config["postgresql_instance_id"]))
    mysql_instance_id = int(str(config["mysql_instance_id"]))

    cases = [
        {
            "name": "pg_prometheus_exact_target",
            "source_type": "prometheus",
            "host_id": f"{host}:9187",
            "labels": {"instance": f"{host}:9187", "host": host, "job": "postgres_exporter"},
            "expected_instance_id": postgresql_instance_id,
        },
        {
            "name": "mysql_prometheus_exact_target",
            "source_type": "prometheus",
            "host_id": f"{host}:9100",
            "labels": {"instance": f"{host}:9100", "host": host, "job": "node_exporter"},
            "expected_instance_id": mysql_instance_id,
        },
        {
            "name": "pg_zabbix_host_name",
            "source_type": "zabbix",
            "host_id": zabbix_hostid,
            "labels": {"hostid": zabbix_hostid, "host": f"postgresql ({host})"},
            "expected_instance_id": postgresql_instance_id,
        },
        {
            "name": "mysql_zabbix_host_name",
            "source_type": "zabbix",
            "host_id": zabbix_hostid,
            "labels": {"hostid": zabbix_hostid, "host": f"mysql ({host})"},
            "expected_instance_id": mysql_instance_id,
        },
    ]
    results = []
    with SessionLocal() as db:
        for case in cases:
            resolved = binding_service.resolve_instance(
                db,
                source_type=case["source_type"],
                host_id=case["host_id"],
                labels=case["labels"],
                raw_data=case["labels"],
            )
            actual = resolved.get("instance_id")
            results.append(
                {
                    "name": case["name"],
                    "expected_instance_id": case["expected_instance_id"],
                    "actual_instance_id": actual,
                    "binding_id": resolved.get("binding_id"),
                    "match_rule": resolved.get("match_rule"),
                    "pass": actual == case["expected_instance_id"],
                }
            )
    failed = [item for item in results if not item["pass"]]
    print(json.dumps({"ok": not failed, "failed": failed, "results": results}, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
