"""MCP scoped API keys table (mcp_api_keys).

Revision ID: 0035_mcp_api_keys
Revises: b4d40e450bb8
Create Date: 2026-07-23

Idempotent, additive: the CREATE TABLE is guarded by an inspector existence
check. No existing table is altered. FK-free table (keys reference no other
row). token_hash carries a unique index. All columns nullable or with a
server_default — no NOT NULL added without a default.

NOTE (down_revision): chains off the live alembic head b4d40e450bb8
(add_caller_key_hash_to_agent_action_log), which lands after 0034
(00cb512bde05). The implementation plan referenced 00cb512bde05 as head;
that was stale by the time this migration was written.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID

revision = "0035_mcp_api_keys"
down_revision = "b4d40e450bb8"
branch_labels = None
depends_on = None

_UUID = sa.Uuid().with_variant(_PG_UUID(as_uuid=True), "postgresql")
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "mcp_api_keys" not in existing_tables:
        op.create_table(
            "mcp_api_keys",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("allowed_tools", _JSONB, server_default="[]", nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("created_by", sa.String(128), server_default="", nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_mcp_api_keys_token_hash", "mcp_api_keys", ["token_hash"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "mcp_api_keys" in set(inspector.get_table_names()):
        op.drop_index("ix_mcp_api_keys_token_hash", table_name="mcp_api_keys")
        op.drop_table("mcp_api_keys")
