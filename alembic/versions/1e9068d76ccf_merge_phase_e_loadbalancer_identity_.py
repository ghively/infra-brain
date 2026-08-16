"""merge phase-e loadbalancer/identity/saas_inventory heads

Revision ID: 1e9068d76ccf
Revises: 092ca5a8b306, 56c6358f9da4, ce988d639c11
Create Date: 2026-07-30 09:12:47.692928

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1e9068d76ccf'
down_revision: Union[str, Sequence[str], None] = ('092ca5a8b306', '56c6358f9da4', 'ce988d639c11')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
