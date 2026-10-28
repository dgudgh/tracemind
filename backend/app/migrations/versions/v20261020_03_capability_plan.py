"""Create the capability plan, plan step and plan revision tables.

Created through ``__table__.create`` for the same reason as ``20261020_02_capability_registry``:
``schema_contract.validate_schema`` would refuse startup over any drift between hand-written DDL
and the model metadata (index names, UniqueConstraint shapes).

The CHECK constraints on ``db_capability_plan`` (status/outcome vocabularies and the
``settled`` ⇔ ``outcome IS NOT NULL`` equivalence) are enforced by SQLite, PostgreSQL and MySQL
>= 8.0.16, but **MySQL before 8.0.16 parses CHECK without enforcing it**. They are an intent
guardrail; the write-path funnel in ``plan_service`` (T03) is the real line of defence.
"""
from __future__ import annotations

REVISION = "20261020_03_capability_plan"
DESCRIPTION = "Create capability plans, steps and revisions"


def upgrade(connection) -> None:
    from app.models.capability_plan import (
        DBCapabilityPlan,
        DBCapabilityPlanRevision,
        DBCapabilityPlanStep,
    )

    for model in (DBCapabilityPlan, DBCapabilityPlanStep, DBCapabilityPlanRevision):
        model.__table__.create(connection, checkfirst=True)


def downgrade(connection) -> None:
    from sqlalchemy import inspect, select

    from app.models.capability_plan import (
        DBCapabilityPlan,
        DBCapabilityPlanRevision,
        DBCapabilityPlanStep,
    )

    # Steps and revisions must go first: dropping the parent table first would orphan rows.
    for model in (DBCapabilityPlanStep, DBCapabilityPlanRevision, DBCapabilityPlan):
        table = model.__table__
        if not inspect(connection).has_table(table.name):
            continue
        key = table.c.plan_id if hasattr(table.c, "plan_id") else table.c.id
        if connection.execute(select(key).limit(1)).first():
            raise RuntimeError(
                "Capability plan history must be retained; non-empty table cannot be downgraded"
            )
        table.drop(connection)
