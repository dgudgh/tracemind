"""Retain structurally gated model proposals without granting any execution path."""
REVISION = "20261020_08_capability_proposal"
DESCRIPTION = "Recover validated model proposals by authenticated request identity"


def upgrade(connection):
    from app.models.capability_proposal import DBCapabilityProposal

    DBCapabilityProposal.__table__.create(connection, checkfirst=True)


def downgrade(connection):
    from sqlalchemy import inspect, select
    from app.models.capability_proposal import DBCapabilityProposal

    table = DBCapabilityProposal.__table__
    if not inspect(connection).has_table(table.name):
        return
    if connection.execute(select(table.c.proposal_id).limit(1)).first():
        raise RuntimeError("Proposal receipts must be retained; non-empty downgrade refused")
    table.drop(connection)
