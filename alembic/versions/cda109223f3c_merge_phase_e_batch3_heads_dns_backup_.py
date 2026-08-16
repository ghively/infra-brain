"""merge phase-e-batch3 heads (dns backup licensing secrets-inventory)

Revision ID: cda109223f3c
Revises: 6dd56fd814ab, a4f6c8d1e2b3, bb2c145b8f38, cf766bd0e005, dd4934a8b610
Create Date: 2026-07-30 10:57:41.934013

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cda109223f3c'
down_revision: Union[str, Sequence[str], None] = ('6dd56fd814ab', 'a4f6c8d1e2b3', 'bb2c145b8f38', 'cf766bd0e005', 'dd4934a8b610')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
