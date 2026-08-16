"""add inventory_reconcile_events

Revision ID: 0002_add_inventory_reconcile
Revises: 0001_initial_schema
Create Date: 2026-06-19 00:00:00.000000

Adds the ``inventory_reconcile_events`` table (InventoryReconcileAgent). Like
0001, this uses ``Base.metadata.create_all`` which is idempotent — it creates
only tables that do not yet exist, so it is safe on both fresh databases
(no-op; everything already created by 0001) and existing deployments at 0001
(adds just the new table).
"""
from alembic import op
from infra_brain.db.models import Base

# revision identifiers, used by Alembic.
revision = "0002_add_inventory_reconcile"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Scoped to the ONE table this revision owns. The docstring above reasons
    # that an unscoped create_all is "safe because it is idempotent" — that
    # holds for re-running THIS revision, but not for the chain as a whole:
    # Base.metadata is the LIVE ORM metadata, so it also creates tables owned
    # by much later revisions, which then hit DuplicateTable when the chain
    # reaches them on a fresh database. That is TRK-312 — migration-parity
    # dying at 87ed7c6601d0 on client_observations, a table this revision
    # predates by months. 0001 already scopes itself via
    # _tables_owned_by_later_migrations(); 0002/0003/0014 did not.
    from infra_brain.db.models import InventoryReconcileEvent

    Base.metadata.create_all(
        bind=op.get_bind(), tables=[InventoryReconcileEvent.__table__]
    )


def downgrade() -> None:
    from infra_brain.db.models import InventoryReconcileEvent

    InventoryReconcileEvent.__table__.drop(bind=op.get_bind(), checkfirst=True)
