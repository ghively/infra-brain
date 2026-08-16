"""trk031 linux posture

Revision ID: 69f971606958
Revises: 9483820d6f52
Create Date: 2026-07-20 12:43:49.607724

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '69f971606958'
down_revision: Union[str, Sequence[str], None] = '9483820d6f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Inspector-guarded (house convention) so this is a no-op on a DB that already
    # carries these tables — fresh-install create_all path or a CI re-run against a
    # non-pristine DB.
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "host_firewall_rules" not in tables:
        op.create_table('host_firewall_rules',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('table_name', sa.String(length=32), nullable=True),
        sa.Column('chain', sa.String(length=64), nullable=True),
        sa.Column('rule_text', sa.Text(), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('resource_id', 'source', 'table_name', 'chain', 'rule_text', name='uq_host_firewall_rule_natkey')
        )
        op.create_index(op.f('ix_host_firewall_rules_resource_id'), 'host_firewall_rules', ['resource_id'], unique=False)

    if "linux_pending_updates" not in tables:
        op.create_table('linux_pending_updates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('host_id', sa.Uuid(), nullable=False),
        sa.Column('package', sa.String(length=256), nullable=False),
        sa.Column('current_version', sa.String(length=128), nullable=True),
        sa.Column('available_version', sa.String(length=128), nullable=True),
        sa.Column('security', sa.Boolean(), nullable=False),
        sa.Column('manager', sa.String(length=16), nullable=True),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['host_id'], ['linux_hosts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('host_id', 'package', 'available_version', name='uq_linux_pending_update_natkey')
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "linux_pending_updates" in tables:
        op.drop_table('linux_pending_updates')
    if "host_firewall_rules" in tables:
        op.drop_index(op.f('ix_host_firewall_rules_resource_id'), table_name='host_firewall_rules')
        op.drop_table('host_firewall_rules')
