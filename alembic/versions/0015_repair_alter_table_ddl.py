"""Repair ALTER TABLE DDL — apply missing column additions to existing tables.

Revision ID: 0015_repair_alter_table_ddl
Revises: 0014_add_custom_views
Create Date: 2026-06-28

Migrations 0001–0003 and 0014 called Base.metadata.create_all() against the
live ORM. On a DB first initialized by 0001, any column added to an *existing*
table in subsequent ORM changes was silently dropped — create_all is
idempotent for tables (never alters) but not for columns.

This migration applies ADD COLUMN IF NOT EXISTS for every column that could
be missing from the live DB:

  * collection_runs.error_message  (Text nullable) — intended by 0004 but
    guarded; belt-and-suspenders here ensures it exists even if 0004's guard
    fired on a fresh-install DB where create_all had already added it and
    then a fresh DB without it skipped 0004's branch.

All ALTER TABLE statements use IF NOT EXISTS (PostgreSQL 9.6+) so they are
safe to re-run. SQLite does not support IF NOT EXISTS on ADD COLUMN in older
versions; we wrap with an inspector guard for compatibility with the test
suite (which uses SQLite).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0015_repair_alter_table_ddl"
down_revision = "0014_add_custom_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    inspector = inspect(bind)

    existing_cols = {c["name"] for c in inspector.get_columns("collection_runs")}
    if "error_message" not in existing_cols:
        if dialect_name == "postgresql":
            op.execute(
                "ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS error_message TEXT"
            )
        else:
            # SQLite: inspector guard above already confirmed the column is absent
            op.add_column(
                "collection_runs",
                sa.Column("error_message", sa.Text(), nullable=True),
            )


def downgrade() -> None:
    # error_message was the intent of 0004; downgrade is a no-op here to avoid
    # double-drop when 0004 is also present in the migration chain.
    pass
