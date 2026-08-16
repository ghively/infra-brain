"""add_saas_inventory_tables — SaaS / API-key inventory (GitLab #103).

Revision ID: 56c6358f9da4
Revises: 9d83fb71e295
Create Date: 2026-07-29 21:23:53.953917

Creates two new tables (metadata only — see
``src/infra_brain/db/models/saas_inventory.py`` module docstring):

  * saas_applications      — one row per known third-party SaaS application,
                              linked to its generic Resource row
                              (name/vendor/category/owner_team/details).
  * saas_api_key_metadata  — one row per API key's METADATA ONLY
                              (key_name/scope/created_at/last_used_at/owner/
                              is_active). No column here may ever hold the
                              key VALUE/secret — enforced structurally (no
                              such column exists) and covered by
                              ``tests/db/test_saas_inventory_models.py``.

Safety checklist:
  - Only CREATE TABLE — no ALTER to existing tables
  - Downgrade is inspector-guarded (drop tables only if they exist)
  - Uses op.create_table() DDL — NOT Base.metadata.create_all()
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "56c6358f9da4"
down_revision: Union[str, Sequence[str], None] = "9d83fb71e295"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ── saas_applications ───────────────────────────────────────────────────
    if "saas_applications" not in existing_tables:
        op.create_table(
            "saas_applications",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "resource_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("resources.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("vendor", sa.String(256), nullable=True),
            sa.Column("category", sa.String(128), nullable=True),
            sa.Column("owner_team", sa.String(256), nullable=True),
            sa.Column(
                "details",
                postgresql.JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "last_seen",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("name", name="saas_applications_name_key"),
            sa.UniqueConstraint("resource_id", name="saas_applications_resource_id_key"),
        )

    # ── saas_api_key_metadata (METADATA ONLY — no secret/value column) ─────
    if "saas_api_key_metadata" not in existing_tables:
        op.create_table(
            "saas_api_key_metadata",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("app_name", sa.String(256), nullable=False),
            sa.Column("key_name", sa.String(256), nullable=False),
            sa.Column("scope", sa.String(256), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("owner", sa.String(256), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.UniqueConstraint("app_name", "key_name", name="uq_saas_api_key_app_key"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "saas_api_key_metadata" in existing_tables:
        op.drop_table("saas_api_key_metadata")
    if "saas_applications" in existing_tables:
        op.drop_table("saas_applications")
