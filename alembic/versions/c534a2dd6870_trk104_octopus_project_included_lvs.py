"""trk104 octopus project included_lvs

Revision ID: c534a2dd6870
Revises: 5ee457cc08f8
Create Date: 2026-07-20 13:48:37.185908

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c534a2dd6870"
down_revision: Union[str, Sequence[str], None] = "5ee457cc08f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # TRK-104: NOT NULL added with a server_default so the ALTER backfills any
    # existing octopus_projects rows (safe on a populated table); ORM inserts use
    # the Python ``default=list``. Inspector-guarded (house convention): no-op if
    # the column already exists.
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("octopus_projects")}
    if "included_library_variable_set_ids" not in cols:
        op.add_column(
            "octopus_projects",
            sa.Column(
                "included_library_variable_set_ids",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
                server_default="[]",
            ),
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("octopus_projects")}
    if "included_library_variable_set_ids" in cols:
        op.drop_column("octopus_projects", "included_library_variable_set_ids")
