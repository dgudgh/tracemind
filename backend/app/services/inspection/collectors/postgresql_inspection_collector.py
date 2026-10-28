from __future__ import annotations

from app.analyzers.rules import Finding
from app.models.db_instance import DBInstance
from app.services.inspection import script_db_checks


def collect(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    return script_db_checks.collect_postgresql_script_findings(
        instance,
        policy_items=policy_items,
    )
