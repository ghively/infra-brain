"""trk102 resource retired_at

Revision ID: a03ae493391b
Revises: 69f971606958
Create Date: 2026-07-20 13:24:52.236794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'a03ae493391b'
down_revision: Union[str, Sequence[str], None] = '69f971606958'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Inspector-guarded (house convention): no-op if the column already exists.
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("resources")}
    if "retired_at" not in cols:
        op.add_column('resources', sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("resources")}
    if "retired_at" in cols:
        op.drop_column('resources', 'retired_at')
