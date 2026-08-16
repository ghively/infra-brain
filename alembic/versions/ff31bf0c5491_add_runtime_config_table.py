"""add runtime_config table

Revision ID: ff31bf0c5491
Revises: f7a29d1b8908
Create Date: 2026-07-30 23:59:06.807941

Adds ``runtime_config`` (db/models/governance.py's ``RuntimeConfig`` — TRK-303
live runtime configuration: admin-editable tuning knobs, encrypted secrets, and
collector on/off toggles). Deliberately a sibling table to
``agent_config_settings`` rather than an extension of it (that table has no
type/secret/audit metadata).

Header (revision / down_revision / Create Date) was produced by
``alembic revision --autogenerate``; the ``op.create_table`` body was filled in
by hand because autogenerate emitted an EMPTY diff: migrations 0002/0014 call
``Base.metadata.create_all(bind=op.get_bind())`` with NO table filter against the
CURRENT ``Base.metadata``, so by the time autogenerate compared model-vs-DB the
table already existed. That is the same condition the existence guard below
protects against — see bb2c145b8f38_add_software_licenses_licensing_table.py,
whose structure this file mirrors exactly.

Safety checklist:
  ✅ Only CREATE TABLE — no ALTER to existing tables
  ✅ Downgrade is inspector-guarded (drop table only if it exists)
  ✅ Existence-guarded CREATE — safe against the 0002/0014 blanket create_all
  ✅ No indexes created (single-column PK only) — no CONCURRENTLY concern
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ff31bf0c5491'
down_revision: Union[str, Sequence[str], None] = 'f7a29d1b8908'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "runtime_config" not in existing_tables:
        op.create_table(
            'runtime_config',
            sa.Column('key', sa.String(length=128), nullable=False),
            sa.Column('value_type', sa.String(length=16), nullable=False),
            sa.Column('value', sa.Text(), nullable=True),
            sa.Column('encrypted_value', sa.LargeBinary(), nullable=True),
            sa.Column('is_secret', sa.Boolean(), nullable=False),
            sa.Column('category', sa.String(length=32), nullable=False),
            sa.Column('updated_by', sa.String(length=128), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('key'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())
    if "runtime_config" in set(inspector.get_table_names()):
        op.drop_table('runtime_config')
