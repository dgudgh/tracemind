from __future__ import annotations

from app.analyzers.rules import Finding
from app.models.db_instance import DBInstance
from app.services import generic_db_ops


def collect(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    return generic_db_ops.inspection_findings(
        instance,
        policy_items=policy_items,
    )
