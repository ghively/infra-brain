"""trk_resource_ownership

Revision ID: 31e8b31f182f
Revises: ec4de4fefbef
Create Date: 2026-07-28 17:15:41.143088

New ``resource_ownership`` overlay table (GitLab issue #116): owner_team,
on_call_rotation, criticality_tier keyed by a real FK to ``resources.id``,
mirroring the existing ``HostPurposeMap`` overlay-table pattern. Purely
additive — no existing table, column, constraint, or index is touched, so
there is no lock/backfill risk and the single NOT NULL column (``source``,
default "") starts empty with every row app-inserted going forward.

Hand-completed after autogenerate produced an empty diff: on a from-empty
chain (CI's migration-parity gate and this local run) an EARLIER migration,
``0002_add_inventory_reconcile``, runs a blanket ``Base.metadata.create_all()``
against *today's* models — which now includes ``resource_ownership`` — so by
the time this revision runs the table already exists and an unguarded
``op.create_table`` fails with DuplicateTable. The ``existing_tables``
inspector guard below is the same pattern and rationale documented in
``ec4de4fefbef_graph_phase2_nodes_edges.py`` and ``0035_mcp_api_keys``. Both
paths converge on identical DDL because ``create_all`` builds from the same
metadata. The guard also feeds
``0001_initial_schema._tables_owned_by_later_migrations()``, which
regex-scans sibling migrations for the ``"<table>" not in existing_tables``
membership-check pattern, so that 0001 stops pre-creating this table itself.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '31e8b31f182f'
down_revision: Union[str, Sequence[str], None] = 'ec4de4fefbef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    existing_tables = set(inspect(op.get_bind()).get_table_names())

    if "resource_ownership" not in existing_tables:
        op.create_table(
            "resource_ownership",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("resource_id", sa.Uuid(), nullable=False),
            sa.Column("owner_team", sa.String(length=256), nullable=True),
            sa.Column("on_call_rotation", sa.String(length=256), nullable=True),
            sa.Column("criticality_tier", sa.String(length=64), nullable=True),
            sa.Column("source", sa.String(length=256), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["resource_id"], ["resources.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("resource_id", name="uq_resource_ownership_resource_id"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("resource_ownership")
