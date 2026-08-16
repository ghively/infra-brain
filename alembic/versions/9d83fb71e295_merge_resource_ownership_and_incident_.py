"""merge resource_ownership and incident_correlation heads

Revision ID: 9d83fb71e295
Revises: 2314097fd962, 31e8b31f182f
Create Date: 2026-07-28 18:54:15.264785

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d83fb71e295'
down_revision: Union[str, Sequence[str], None] = ('2314097fd962', '31e8b31f182f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
