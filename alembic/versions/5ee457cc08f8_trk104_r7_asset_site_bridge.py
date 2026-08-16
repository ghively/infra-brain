"""trk104 r7 asset site bridge

Revision ID: 5ee457cc08f8
Revises: f4ddc6f3abe3
Create Date: 2026-07-20 13:45:29.112618

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "5ee457cc08f8"
down_revision: Union[str, Sequence[str], None] = "f4ddc6f3abe3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Inspector-guarded (house convention): no-op if the table already exists.
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "r7_asset_sites" not in tables:
        op.create_table(
            "r7_asset_sites",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("r7_asset_id", sa.Integer(), nullable=False),
            sa.Column("r7_site_id", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("r7_asset_id", "r7_site_id", name="uq_r7_asset_site"),
        )
        op.create_index(
            op.f("ix_r7_asset_sites_r7_asset_id"), "r7_asset_sites", ["r7_asset_id"], unique=False
        )
        op.create_index(
            op.f("ix_r7_asset_sites_r7_site_id"), "r7_asset_sites", ["r7_site_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "r7_asset_sites" in tables:
        op.drop_index(op.f("ix_r7_asset_sites_r7_site_id"), table_name="r7_asset_sites")
        op.drop_index(op.f("ix_r7_asset_sites_r7_asset_id"), table_name="r7_asset_sites")
        op.drop_table("r7_asset_sites")
