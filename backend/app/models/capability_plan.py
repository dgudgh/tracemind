"""Typed plans, their steps and their revisions (U02).

A plan is a long-lived object of *intent*: the user can inspect it, it can need approval, and
it can be revised. That is a different shape from ``DBInspectionJob`` (a short-lived claim +
lease + fencing object owned by a worker), which is why this is a parallel table with a typed
``result_ref_json`` reference, not a generalisation of the job table (design D6).

State machine (design §2.2) - the two sets are disjoint **by construction**:

    status  in {draft, validated, awaiting_approval, approved, running, settled}
    outcome in {succeeded, partial, failed, cancelled, expired, unknown,
                plan_only, blocked, unsupported, needs_input}

``status='settled'`` is only legal together with a non-NULL ``outcome``. Both the CHECKs below
say so. Note on the CHECKs: **MySQL before 8.0.16 parses but does not enforce CHECK
constraints**, so these are an *intent* guardrail, not the only line of defence. The real
enforcement is the write-path funnel in ``plan_service`` (T03: only ``declare_outcome`` /
``settle`` may write ``outcome``) plus the test assertions of design §9. Do not cite the DDL
alone as the guarantee.

No foreign keys, consistent with the rest of the platform.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityPlan(Base):
    __tablename__ = "db_capability_plan"

    plan_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    # Frozen reference to the definition this plan was created under. The executor reads the
    # snapshot as its frozen semantics. Execution still checks current registry/version,
    # permissions and disable switches; a frozen plan is never a bypass for revocation.
    capability_id: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # NULL means a global capability (no instance in scope).
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # From the auth token, never from the request body (the ISS-55 discipline).
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_declared_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Approval/withdrawal fields are reserved for U03 (design §2.5). batch-2 only ever writes
    # `not_required` / `pending` and never transitions a plan to `approved`.
    approval_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_required"
    )
    # Fingerprint of (capability_snapshot + target_scope + params), bound to a future approval.
    # Written ONLY when approval_state != 'not_required' - a not-required plan has nothing to
    # bind, and a hash over nothing would be fabricated evidence. No binding means NULL, and
    # any later parameter/target change yields a different hash, voiding the approval bound to
    # the old one. A default or placeholder value is forbidden (see the test that pins this).
    approval_binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Idempotency (design §2.4): same key = same request; an explicit `Idempotency-Key` header
    # is an explicit re-run and produces a new plan whose `parent_plan_id` points at the
    # original. `parent_plan_id UNIQUE` means one re-run per parent.
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    parent_plan_id: Mapped[str | None] = mapped_column(String(30), unique=True, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Typed reference to the run's product (design §2.1), dispatched by `kind` only:
    # {"kind": "inspection_job", "id": ..., "schema": "run_read/v1"} | {"kind":
    # "capability_matrix", ...} | {"kind": "inspection_policy_list", ...}.
    result_ref_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','validated','awaiting_approval','approved','running','settled')",
            name="ck_db_capability_plan_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('succeeded','partial','failed','cancelled','expired','unknown',"
            "'plan_only','blocked','unsupported','needs_input')",
            name="ck_db_capability_plan_outcome",
        ),
        CheckConstraint(
            "(status = 'settled') = (outcome IS NOT NULL)",
            name="ck_db_capability_plan_settled_outcome",
        ),
    )


class DBCapabilityPlanStep(Base):
    __tablename__ = "db_capability_plan_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # Topological order *is* the seq order: a step may only depend on strictly smaller seqs,
    # which makes cycles inexpressible (design §2.3).
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    depends_on_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    side_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    precondition_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    outcome_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    result_ref_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("plan_id", "seq", name="uq_db_capability_plan_step_plan_seq"),
    )


class DBCapabilityPlanRevision(Base):
    __tablename__ = "db_capability_plan_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "plan_id", "revision", name="uq_db_capability_plan_revision_plan_revision"
        ),
    )
