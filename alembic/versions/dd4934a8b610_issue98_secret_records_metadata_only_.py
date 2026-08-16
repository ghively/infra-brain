"""issue98 secret_records metadata only inventory

Revision ID: dd4934a8b610
Revises: 9d83fb71e295
Create Date: 2026-07-30 10:03:29.643453

GitLab issue #98 (secrets-manager inventory, metadata only): new
``secret_records`` table backing SecretsInventoryAgent
(``src/infra_brain/agents/secrets_inventory.py``), keyed on
(backend, secret_path) per db/models/secrets_inventory.py's ``SecretRecord``.
Deliberately no value/encrypted-value/hash column — metadata only.

Guarded with an inspector ``if "secret_records" not in existing_tables:``
check (0001_initial_schema's established convention for a brand-new table —
same pattern as 6dd56fd814ab_add_backup_jobs_table.py and
a4f6c8d1e2b3_dns_zone_record_issue95.py): several early migrations
(0002/0003/0014/31e8b31f182f) call a blanket
``Base.metadata.create_all(bind=op.get_bind())`` with no table filter, which
on a from-empty DB precreates ANY table current models declare —
``secret_records`` included, once db/models/secrets_inventory.py exists —
before this migration ever runs. Without the guard, the unconditional
``op.create_table`` raises ``DuplicateTable`` on that fresh-install path
(confirmed via /pg-gate-check on the phase-e-batch3 integration branch).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'dd4934a8b610'
down_revision: Union[str, Sequence[str], None] = '9d83fb71e295'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "secret_records" not in existing_tables:
        op.create_table('secret_records',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('backend', sa.String(length=32), nullable=False),
        sa.Column('secret_path', sa.String(length=512), nullable=False),
        sa.Column('last_rotated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('referencing_system', sa.String(length=256), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('backend', 'secret_path', name='uq_secret_records_backend_path')
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "secret_records" in existing_tables:
        op.drop_table('secret_records')
