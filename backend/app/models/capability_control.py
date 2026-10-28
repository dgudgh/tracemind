"""Typed control commands, local target generations and append-only reconciliation evidence."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import NotRequired, TypedDict

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Enum, Integer, JSON, String,
    UniqueConstraint, event, inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.database import Base


class ControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    PROBE = "probe"


class CommandStatus(StrEnum):
    ACCEPTED = "accepted"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class AckStage(StrEnum):
    RECEIVED = "received"
    QUIESCED = "quiesced"
    TERMINATED = "terminated"
    NOT_STARTED = "not_started"
    RESUMED = "resumed"
    OBSERVED = "observed"
    COMPLETED = "completed"
    REJECTED = "rejected"


class ObservedState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class HoldState(StrEnum):
    UNCLAIMED = "unclaimed"
    HELD = "held"
    PAUSED = "paused"
    STOP_PENDING = "stop_pending"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RELEASED = "released"
    LEGACY_UNVERIFIED = "legacy_unverified"


class Resolution(StrEnum):
    UNRESOLVED = "unresolved"
    CONFIRMED_NOT_STARTED = "confirmed_not_started"
    CONFIRMED_COMPLETED = "confirmed_completed"
    CONFIRMED_STOPPED = "confirmed_stopped"
    STILL_RUNNING = "still_running"
    DISPUTED = "disputed"


class RuntimeObservation(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    QUIESCED = "quiesced"
    TERMINATED = "terminated"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class EffectOutcome(StrEnum):
    NO_EFFECT = "no_effect"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ControlEvidenceRef(TypedDict):
    kind: str
    id: str
    schema: str
    sha256: NotRequired[str]


EVIDENCE_KINDS = frozenset({
    "control_receipt", "checkpoint", "domain_run", "inspection_job", "agent_command",
    "target_observation", "reconciliation",
})


def _enum(kind, name):
    return Enum(kind, name=name, native_enum=False, create_constraint=True,
                validate_strings=True, values_callable=lambda values: [item.value for item in values])


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 hash")
    return value


def _reference(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {"kind", "id", "schema", "sha256"}
            or not {"kind", "id", "schema"} <= set(value)
            or not isinstance(value["kind"], str) or value["kind"] not in EVIDENCE_KINDS):
        raise ValueError("Expected a typed control evidence pointer")
    for key, limit in (("id", 128), ("schema", 128)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
            raise ValueError("Invalid evidence pointer field")
    if "sha256" in value:
        _hash(value["sha256"])
    return dict(value)


class DBCapabilityControlCommand(Base):
    __tablename__ = "db_capability_control_command"

    command_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    execution_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action: Mapped[ControlAction] = mapped_column(_enum(ControlAction, "ck_control_action"), nullable=False)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expected_control_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[CommandStatus] = mapped_column(
        _enum(CommandStatus, "ck_control_status"), nullable=False, default=CommandStatus.ACCEPTED)
    ack_stage: Mapped[AckStage | None] = mapped_column(_enum(AckStage, "ck_control_ack_stage"))
    observed_state: Mapped[ObservedState | None] = mapped_column(_enum(ObservedState, "ck_control_observation"))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ack_evidence_ref: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    __table_args__ = (
        UniqueConstraint("requested_by", "idempotency_key", name="uq_control_request"),
        UniqueConstraint("execution_id", "control_version", name="uq_control_execution_version"),
        CheckConstraint("execution_id > 0 AND requested_by > 0 AND plan_revision > 0 "
                        "AND length(binding_hash) = 64 AND length(input_hash) = 64 "
                        "AND length(reason) > 0 AND length(idempotency_key) > 0 "
                        "AND length(plan_id) > 0 AND length(target_key) > 0", name="ck_control_binding"),
        CheckConstraint("expected_control_version >= 0 AND control_version = expected_control_version + 1 "
                        "AND fencing_epoch > 0 AND delivery_attempts >= 0", name="ck_control_version"),
        CheckConstraint("deadline_at > created_at", name="ck_control_deadline"),
        CheckConstraint("ack_stage IS NULL OR ack_stage = 'received' OR "
                        "(ack_evidence_ref IS NOT NULL AND acknowledged_at IS NOT NULL)",
                        name="ck_control_ack_evidence"),
        CheckConstraint("status != 'applied' OR (applied_at IS NOT NULL AND ack_stage IS NOT NULL "
                        "AND observed_state IS NOT NULL AND ack_evidence_ref IS NOT NULL "
                        "AND acknowledged_at IS NOT NULL AND ("
                        "(action = 'pause' AND ack_stage = 'quiesced' AND observed_state = 'paused') OR "
                        "(action = 'stop' AND ack_stage IN ('terminated','not_started') "
                        "AND observed_state = 'stopped') OR "
                        "(action = 'resume' AND ack_stage = 'resumed' AND observed_state = 'running') OR "
                        "(action = 'probe' AND ack_stage = 'observed')))", name="ck_control_applied"),
    )

    @validates("binding_hash", "input_hash")
    def _validate_hash(self, key, value):
        return _hash(value)

    @validates("ack_evidence_ref")
    def _validate_reference(self, key, value):
        return _reference(value)


class DBCapabilityTargetFence(Base):
    __tablename__ = "db_capability_target_fence"

    target_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    epoch: Mapped[int | None] = mapped_column(BigInteger)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    hold_state: Mapped[HoldState] = mapped_column(_enum(HoldState, "ck_target_hold_state"), nullable=False)
    holder_execution_id: Mapped[int | None] = mapped_column(Integer, index=True)
    holder_plan_id: Mapped[str | None] = mapped_column(String(30))
    binding_hash: Mapped[str | None] = mapped_column(String(64))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    hold_reason: Mapped[str] = mapped_column(String(96), nullable=False)
    release_evidence_ref: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    __table_args__ = (
        CheckConstraint("row_version >= 0 AND (epoch IS NULL OR epoch > 0) "
                        "AND length(target_key) > 0 AND length(hold_reason) > 0 "
                        "AND (holder_execution_id IS NULL OR holder_execution_id > 0) "
                        "AND (binding_hash IS NULL OR length(binding_hash) = 64)", name="ck_target_version"),
        CheckConstraint("hold_state = 'legacy_unverified' OR epoch IS NOT NULL", name="ck_target_epoch"),
        CheckConstraint("hold_state NOT IN ('held','paused','stop_pending','reconciliation_required') OR "
                        "(holder_execution_id IS NOT NULL AND holder_plan_id IS NOT NULL "
                        "AND binding_hash IS NOT NULL AND acquired_at IS NOT NULL "
                        "AND released_at IS NULL AND release_evidence_ref IS NULL)", name="ck_target_holder"),
        CheckConstraint("hold_state NOT IN ('unclaimed','released') OR "
                        "(holder_execution_id IS NULL AND holder_plan_id IS NULL AND binding_hash IS NULL)",
                        name="ck_target_free_holder"),
        CheckConstraint("hold_state != 'unclaimed' OR (acquired_at IS NULL AND released_at IS NULL "
                        "AND release_evidence_ref IS NULL)", name="ck_target_unclaimed"),
        CheckConstraint("hold_state != 'released' OR (released_at IS NOT NULL "
                        "AND release_evidence_ref IS NOT NULL)", name="ck_target_release"),
    )

    @validates("binding_hash")
    def _validate_hash(self, key, value):
        return None if value is None else _hash(value)

    @validates("release_evidence_ref")
    def _validate_reference(self, key, value):
        return _reference(value)


class DBCapabilityReconciliation(Base):
    __tablename__ = "db_capability_reconciliation"

    decision_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    execution_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    observed_epoch: Mapped[int | None] = mapped_column(BigInteger)
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_decision_id: Mapped[str | None] = mapped_column(String(30))
    resolution: Mapped[Resolution] = mapped_column(_enum(Resolution, "ck_reconcile_resolution"), nullable=False)
    runtime_observation: Mapped[RuntimeObservation] = mapped_column(
        _enum(RuntimeObservation, "ck_reconcile_runtime"), nullable=False)
    effect_outcome: Mapped[EffectOutcome] = mapped_column(_enum(EffectOutcome, "ck_reconcile_effect"), nullable=False)
    recorded_by: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_by: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    runtime_evidence_ref: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    effect_evidence_ref: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    barrier_evidence_ref: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    release_target: Mapped[bool] = mapped_column(Boolean(create_constraint=True), nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_reconcile_execution_sequence"),
        CheckConstraint("execution_id > 0 AND plan_revision > 0 AND control_version >= 0 "
                        "AND recorded_by > 0 AND length(binding_hash) = 64 AND length(reason) > 0 "
                        "AND length(plan_id) > 0 AND length(target_key) > 0 "
                        "AND (observed_epoch IS NULL OR observed_epoch > 0) "
                        "AND (reviewed_by IS NULL OR (reviewed_by > 0 AND reviewed_by != recorded_by))",
                        name="ck_reconcile_binding"),
        CheckConstraint("(sequence = 1 AND previous_decision_id IS NULL) OR "
                        "(sequence > 1 AND previous_decision_id IS NOT NULL "
                        "AND previous_decision_id != decision_id)", name="ck_reconcile_sequence"),
        CheckConstraint("resolution != 'confirmed_not_started' OR "
                        "(runtime_observation = 'not_started' AND effect_outcome = 'no_effect')",
                        name="ck_reconcile_not_started"),
        CheckConstraint("resolution != 'confirmed_completed' OR "
                        "(runtime_observation = 'completed' AND effect_outcome NOT IN ('unknown','no_effect'))",
                        name="ck_reconcile_completed"),
        CheckConstraint("resolution != 'confirmed_stopped' OR runtime_observation IN ('terminated','quiesced')",
                        name="ck_reconcile_stopped"),
        CheckConstraint("resolution != 'still_running' OR runtime_observation = 'running'",
                        name="ck_reconcile_running"),
        CheckConstraint("NOT release_target OR (resolution IN "
                        "('confirmed_not_started','confirmed_completed','confirmed_stopped') "
                        "AND runtime_observation IN ('not_started','completed','terminated','quiesced') "
                        "AND effect_outcome != 'unknown' AND observed_epoch IS NOT NULL "
                        "AND runtime_evidence_ref IS NOT NULL AND effect_evidence_ref IS NOT NULL "
                        "AND barrier_evidence_ref IS NOT NULL AND source_verified_at IS NOT NULL "
                        "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)", name="ck_reconcile_release"),
    )

    @validates("binding_hash")
    def _validate_hash(self, key, value):
        return _hash(value)

    @validates("runtime_evidence_ref", "effect_evidence_ref", "barrier_evidence_ref")
    def _validate_reference(self, key, value):
        return _reference(value)


@event.listens_for(DBCapabilityReconciliation, "before_update")
@event.listens_for(DBCapabilityReconciliation, "before_delete")
def _reject_reconciliation_mutation(mapper, connection, target):
    raise ValueError("Reconciliation decisions are append-only")


@event.listens_for(DBCapabilityControlCommand, "before_update")
def _freeze_command_intent(mapper, connection, target):
    mutable = {"status", "ack_stage", "observed_state", "last_sent_at", "acknowledged_at",
               "applied_at", "delivery_attempts", "ack_evidence_ref"}
    if any(attr.history.has_changes() for attr in inspect(target).attrs if attr.key not in mutable):
        raise ValueError("Control command intent is immutable")


@event.listens_for(DBCapabilityControlCommand, "before_delete")
@event.listens_for(DBCapabilityTargetFence, "before_delete")
def _retain_control_history(mapper, connection, target):
    raise ValueError("Control commands and target epochs must be retained")
