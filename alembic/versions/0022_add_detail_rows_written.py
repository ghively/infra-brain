"""Add detail_rows_written column to collection_runs.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-30

Rich collectors (octopus, linux, windows, vsphere, iac, vuln) persist most of
their data in a detail-write phase AFTER the generic ``Resource`` upsert. The
base ``run()`` only counted rows from the generic collect() loop, so domains
that write exclusively to detail tables reported ``resources_found=0`` and
appeared dead in collection-health even when they had data.

This migration adds a nullable ``detail_rows_written`` integer column to
``collection_runs`` (default 0). ``BaseAgent._write_details`` now accumulates
and persists the row count returned by each detail-write function. The sum of
``resources_found + detail_rows_written`` is the total persisted row count for
the run and is surfaced in the collection-health view.

Safety checklist:
  ✅ Nullable column with default 0 — no NOT NULL added to existing tables
  ✅ No DROP TABLE / DROP COLUMN
  ✅ Idempotent (existence-checked before adding)
  ✅ Downgrade drops the column (additive-only; safe)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    tables = set(inspector.get_table_names())
    if "collection_runs" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("collection_runs")}
    if "detail_rows_written" not in existing_cols:
        op.add_column(
            "collection_runs",
            sa.Column("detail_rows_written", sa.Integer(), nullable=True, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    tables = set(inspector.get_table_names())
    if "collection_runs" not in tables:
        return

    existing_cols = {c["name"] for c in inspector.get_columns("collection_runs")}
    if "detail_rows_written" in existing_cols:
        op.drop_column("collection_runs", "detail_rows_written")
