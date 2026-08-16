"""instinct_zone — add nullable zone column to instincts table.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Adds a nullable ``zone VARCHAR(32) DEFAULT 'corporate'`` column to the
``instincts`` table so promoted instincts can be tagged to a trust zone
(corporate vs HSA) for routing to the correct specialist agent.

The column is nullable on first add; existing rows default to 'corporate'.
No NOT NULL constraint is imposed so the migration is safe on a live table.

Safety checklist:
  ✅ Column is nullable — no NOT NULL added to existing rows
  ✅ No DROP TABLE or DROP COLUMN on existing tables in upgrade
  ✅ Downgrade is inspector-guarded (idempotent)
  ✅ Uses op.add_column() DDL — NOT Base.metadata.create_all()
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {col["name"] for col in inspector.get_columns("instincts")}
    if "zone" not in cols:
        # NOTE: in-place edit (2026-06-30, Task 4.3) — server_default changed
        # from 'corporate' to 'corpor' to match ZONE_CORPORATE (db/models/core.py).
        # This only helps fresh/first-run DBs (create_all / migrate-from-base);
        # the live-volume convergence for already-migrated DBs is handled by the
        # fresh-head repair revision 0024_zone_canonicalize (a7b8c9d0e1f2).
        op.add_column(
            "instincts",
            sa.Column(
                "zone",
                sa.String(32),
                nullable=True,
                server_default="corpor",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {col["name"] for col in inspector.get_columns("instincts")}
    if "zone" in cols:
        op.drop_column("instincts", "zone")
