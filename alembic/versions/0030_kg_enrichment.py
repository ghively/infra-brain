"""MR-H (kg-enrichment): edge soft-delete status + net/cloud/k8s identity FKs.

Revision ID: 8de46a9884f2
Revises: d1e2f3a4b5c6
Create Date: 2026-07-07

Two schema additions for MR-H:

1. ``resource_relationships.status`` (String(16), NOT NULL default 'active').
   Backs the IS_SAME_AS confidence-decay data-loss fix (KG-6): a decaying
   identity edge that crosses the low-confidence floor is now moved to
   ``status='archived'`` (soft-deleted, reviewable/restorable) instead of
   being HARD DELETED, which permanently lost a potentially-still-valid link.
   A *partial* index (``WHERE status = 'archived'``) backs the operator-review
   query path without adding an unhelpful full btree on a 2-value column.

2. ``host_identities.{net,cloud,k8s}_resource_id`` (nullable FKs). Extends
   cross-source identity reconciliation (KG-6) to netdiscovery/net, cloud, and
   k8s resources, which HostReconcileAgent previously ignored.

Both are additive and nullable / defaulted, so no backfill or NOT-NULL lock
risk on existing rows. Postgres-only bits (partial index CONCURRENTLY) are
guarded; SQLite (test engine) gets the column via batch_alter_table.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "8de46a9884f2"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

_ARCHIVED_IDX = "ix_rr_status_archived"


def _cols(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. resource_relationships.status ---------------------------------------
    if "resource_relationships" in tables and "status" not in _cols(
        inspector, "resource_relationships"
    ):
        op.add_column(
            "resource_relationships",
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            ),
        )
        # Partial index for the rare archived rows (operator review path).
        if bind.dialect.name == "postgresql":
            with op.get_context().autocommit_block():
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_ARCHIVED_IDX} "
                    "ON resource_relationships (status) WHERE status = 'archived'"
                )

    # 2. host_identities net/cloud/k8s resource_id FKs -----------------------
    if "host_identities" in tables:
        existing = _cols(inspector, "host_identities")
        new_cols = [
            "net_resource_id",
            "cloud_resource_id",
            "k8s_resource_id",
        ]
        to_add = [c for c in new_cols if c not in existing]
        if to_add:
            with op.batch_alter_table("host_identities") as batch:
                for col in to_add:
                    batch.add_column(
                        sa.Column(col, sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True)
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "host_identities" in tables:
        existing = _cols(inspector, "host_identities")
        with op.batch_alter_table("host_identities") as batch:
            for col in ("k8s_resource_id", "cloud_resource_id", "net_resource_id"):
                if col in existing:
                    batch.drop_column(col)

    if "resource_relationships" in tables:
        if bind.dialect.name == "postgresql":
            with op.get_context().autocommit_block():
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ARCHIVED_IDX}")
        if "status" in _cols(inspector, "resource_relationships"):
            op.drop_column("resource_relationships", "status")
