"""closed-loop tables (proposed_actions, compliance_violations, root_cause_notes)

Revision ID: 0003_closed_loop_tables
Revises: 0002_add_inventory_reconcile
Create Date: 2026-06-20 00:00:00.000000

Idempotent ``create_all`` (only creates missing tables) — adds the tables for
the closed-loop agents (RemediationAgent, ComplianceAgent, RootCauseAgent).
"""
from alembic import op
from infra_brain.db.models import Base

revision = "0003_closed_loop_tables"
down_revision = "0002_add_inventory_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ONLY the three tables this revision owns — never a blanket
    # create_all. Base.metadata is the LIVE ORM metadata, so a bare
    # create_all() here creates every table the models will ever have,
    # including ones introduced by much later revisions. Replaying the chain
    # on a fresh DB then died at 87ed7c6601d0 with
    # DuplicateTable: relation "client_observations" already exists —
    # a table this revision has no business knowing about. That is TRK-312:
    # migration-parity failing deterministically even from a clean database.
    # 0001 already guards against exactly this via
    # _tables_owned_by_later_migrations(); this revision simply never did.
    # The explicit list mirrors downgrade() below, so both directions move the
    # same three tables.
    from infra_brain.db.models import ComplianceViolation, ProposedAction, RootCauseNote

    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            ProposedAction.__table__,
            ComplianceViolation.__table__,
            RootCauseNote.__table__,
        ],
    )


def downgrade() -> None:
    from infra_brain.db.models import ComplianceViolation, ProposedAction, RootCauseNote

    for model in (RootCauseNote, ComplianceViolation, ProposedAction):
        model.__table__.drop(bind=op.get_bind(), checkfirst=True)
