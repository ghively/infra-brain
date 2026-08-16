"""add_backup_jobs_table

Revision ID: 6dd56fd814ab
Revises: 9d83fb71e295
Create Date: 2026-07-30 10:00:54.774904

GitLab issue #96 (backup verification): new ``backup_jobs`` table backing
BackupAgent (``src/infra_brain/agents/backup.py``), keyed on
(resource_id, backend, job_name) per db/models/backup.py's ``BackupJob``.

Guarded with an inspector ``if "backup_jobs" not in existing_tables:`` check
(0001_initial_schema's established convention for a brand-new table — see
0031_inv4_posture_purpose_map_longtail.py for the same pattern): several
early migrations (0002/0003/0014/31e8b31f182f) call a blanket
``Base.metadata.create_all(bind=op.get_bind())`` with no table filter, which
on a from-empty DB precreates ANY table current models declare — including
``backup_jobs`` now that db/models/backup.py exists — before this migration
ever runs. Without the guard, this migration's unconditional
``op.create_table`` raises ``DuplicateTable`` on that fresh-install path
(confirmed while authoring this migration: a from-empty ``alembic upgrade
head`` run failed exactly this way). The guard makes this migration
idempotent regardless of which path created the table first, matching
0001's own ``_tables_owned_by_later_migrations()`` exclusion regex (Pattern
2), so 0001 correctly excludes ``backup_jobs`` from its own create_all and
this migration becomes the single source of truth for its DDL going forward.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '6dd56fd814ab'
down_revision: Union[str, Sequence[str], None] = '9d83fb71e295'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "backup_jobs" not in existing_tables:
        op.create_table(
            'backup_jobs',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('resource_id', sa.Uuid(), nullable=False),
            sa.Column('backend', sa.String(length=32), nullable=False),
            sa.Column('job_name', sa.String(length=256), nullable=False),
            sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_restore_test_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('retention_days', sa.Integer(), nullable=True),
            sa.Column('last_status', sa.String(length=32), nullable=True),
            sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('resource_id', 'backend', 'job_name', name='uq_backup_job_natkey'),
        )
        op.create_index(
            op.f('ix_backup_jobs_resource_id'), 'backup_jobs', ['resource_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "backup_jobs" in existing_tables:
        op.drop_index(op.f('ix_backup_jobs_resource_id'), table_name='backup_jobs')
        op.drop_table('backup_jobs')
