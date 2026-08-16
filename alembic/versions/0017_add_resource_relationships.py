"""add resource_relationships — universal knowledge-graph edge table.

Revision ID: 8f3a2b1c9d4e
Revises: 0016_add_host_identities
Create Date: 2026-06-28

Adds ``resource_relationships`` (migration 0017):

  * NEW table: resource_relationships
    - id: UUID primary key
    - from_resource_id: UUID FK → resources.id CASCADE DELETE
    - to_resource_id:   UUID FK → resources.id CASCADE DELETE
    - relationship_type: String(64), not null
    - properties: JSONB, not null, server_default '{}'
    - confidence: Float, not null, server_default 1.0
    - source: String(128), not null
    - created_at: timestamptz server_default now()
    - last_seen:  timestamptz server_default now()

  * Unique constraint: (from_resource_id, to_resource_id, relationship_type)
  * Indexes: ix_rr_from, ix_rr_to, ix_rr_type, ix_rr_from_type

Safety checklist:
  ✅ New table only — no NOT NULL added to existing tables
  ✅ No DROP TABLE or DROP COLUMN on existing tables
  ✅ No CONCURRENTLY concern — new table, no concurrent traffic
  ✅ No lock_timeout concern — new table
  ✅ Downgrade present and idempotent (inspect guard)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "8f3a2b1c9d4e"
down_revision = "0016_add_host_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect

    bind = op.get_bind()
    inspector = inspect(bind)

    if "resource_relationships" not in inspector.get_table_names():
        op.create_table(
            "resource_relationships",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "from_resource_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "to_resource_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column("relationship_type", sa.String(length=64), nullable=False),
            sa.Column(
                "properties",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "confidence",
                sa.Float(),
                nullable=False,
                server_default="1.0",
            ),
            sa.Column("source", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "last_seen",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["from_resource_id"],
                ["resources.id"],
                ondelete="CASCADE",
                name="fk_rr_from_resource",
            ),
            sa.ForeignKeyConstraint(
                ["to_resource_id"],
                ["resources.id"],
                ondelete="CASCADE",
                name="fk_rr_to_resource",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "from_resource_id",
                "to_resource_id",
                "relationship_type",
                name="uq_resource_relationships",
            ),
        )
        op.create_index("ix_rr_from", "resource_relationships", ["from_resource_id"])
        op.create_index("ix_rr_to", "resource_relationships", ["to_resource_id"])
        op.create_index("ix_rr_type", "resource_relationships", ["relationship_type"])
        op.create_index(
            "ix_rr_from_type",
            "resource_relationships",
            ["from_resource_id", "relationship_type"],
        )


def downgrade() -> None:
    from sqlalchemy import inspect

    bind = op.get_bind()
    inspector = inspect(bind)

    if "resource_relationships" in inspector.get_table_names():
        op.drop_index("ix_rr_from_type", table_name="resource_relationships")
        op.drop_index("ix_rr_type", table_name="resource_relationships")
        op.drop_index("ix_rr_to", table_name="resource_relationships")
        op.drop_index("ix_rr_from", table_name="resource_relationships")
        op.drop_table("resource_relationships")
