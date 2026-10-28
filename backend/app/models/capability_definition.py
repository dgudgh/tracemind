"""The runtime ledger for registered capabilities.

The code-side registry (``app/services/capability_registry``, batch-2 T02) is the semantic
declaration; this table is what it *syncs to*: which definitions are currently applied, at
which version, with which content hash, and whether an operator has switched one off.
``definition_hash`` is how "the code changed but nobody re-synced" becomes detectable instead
of invisible.

No foreign keys, consistent with the rest of the platform: cross-table integrity is enforced
by the service layer, not by multi-dialect FK behaviour.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityDefinition(Base):
    __tablename__ = "db_capability_definition"

    capability_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of the declaration payload (title/engines/action_key/budget/...), so a code change
    # that was never synced shows up as a mismatch instead of a silent semantic drift.
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    execution: Mapped[str] = mapped_column(String(8), nullable=False)
    # Capabilities can only be turned off, never re-enabled by a row alone (design D1/D4):
    # the declaration still has to exist in code for `enabled=1` to mean anything.
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    # Stored for conflict comparison only; derived maturity wins over this and neither is
    # shown to callers as-is (design D4).
    declared_maturity_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    catalog_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
