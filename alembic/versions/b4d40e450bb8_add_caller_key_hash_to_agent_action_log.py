"""add caller_key_hash to agent_action_log

Revision ID: b4d40e450bb8
Revises: 1562b407c9cb
Create Date: 2026-07-23 12:09:55.082458

Guarded with inspector existence checks (same idempotency pattern as
0033_collection_run_sweep_id.py): 0001_initial_schema.py's restricted
create_all() builds agent_action_log (an existing table it owns) from
the CURRENT live model, which already includes caller_key_hash — so a
plain op.add_column() here would fail with "duplicate column" whenever
the schema is built via create_all() first (e.g. test_migration_parity.py,
test_migration_zone.py) and the migration chain is then replayed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'b4d40e450bb8'
down_revision: Union[str, Sequence[str], None] = '1562b407c9cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'agent_action_log'
_COLUMN = 'caller_key_hash'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
