"""trk104 vsphere vm resource_pool_moref

Revision ID: f4ddc6f3abe3
Revises: a03ae493391b
Create Date: 2026-07-20 13:40:50.125344

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "f4ddc6f3abe3"
down_revision: Union[str, Sequence[str], None] = "a03ae493391b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Inspector-guarded (house convention): no-op if the column already exists.
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("vsphere_vms")}
    if "resource_pool_moref" not in cols:
        op.add_column(
            "vsphere_vms", sa.Column("resource_pool_moref", sa.String(length=128), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("vsphere_vms")}
    if "resource_pool_moref" in cols:
        op.drop_column("vsphere_vms", "resource_pool_moref")
