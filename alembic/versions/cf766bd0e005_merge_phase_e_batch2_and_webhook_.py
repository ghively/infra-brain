"""merge phase-e-batch2 and webhook-subscriptions heads

Revision ID: cf766bd0e005
Revises: 1e9068d76ccf, a1c2f9e77b31
Create Date: 2026-07-30 10:41:13.749901

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cf766bd0e005'
down_revision: Union[str, Sequence[str], None] = ('1e9068d76ccf', 'a1c2f9e77b31')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
