"""identity_principal_audit issue102

Revision ID: ce988d639c11
Revises: 9d83fb71e295
Create Date: 2026-07-29 18:07:11.984259

Note: 0002/0003/0014/0026 seed a fresh DB via ``Base.metadata.create_all()``
against the LIVE ORM (not a frozen snapshot), so on a brand-new install these
tables already exist by the time this migration runs. Every create is
therefore guarded with the inspector — mirrors 0009_cloud_k8s_net_relational's
established pattern for the exact same reason (and 092ca5a8b306/
56c6358f9da4's own guards, this migration's Phase E siblings). Caught by
pg-gate-check during integration: running the full historical migration
chain end-to-end against a fresh database (not just this migration in
isolation) surfaced the DuplicateTable error the unguarded original raised.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ce988d639c11'
down_revision: Union[str, Sequence[str], None] = '9d83fb71e295'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "identity_principals" not in existing_tables:
        op.create_table('identity_principals',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('external_id', sa.String(length=256), nullable=False),
        sa.Column('username', sa.String(length=256), nullable=False),
        sa.Column('email', sa.String(length=256), nullable=True),
        sa.Column('display_name', sa.String(length=256), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('is_service_account', sa.Boolean(), nullable=False),
        sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'external_id', name='uq_identity_principal_natkey')
        )
        op.create_index('ix_identity_principals_email', 'identity_principals', ['email'], unique=False)
        op.create_index('ix_identity_principals_username', 'identity_principals', ['username'], unique=False)

    if "identity_group_memberships" not in existing_tables:
        op.create_table('identity_group_memberships',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('principal_id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('group_external_id', sa.String(length=256), nullable=False),
        sa.Column('group_name', sa.String(length=256), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['principal_id'], ['identity_principals.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('principal_id', 'source', 'group_external_id', name='uq_identity_group_membership_natkey')
        )
        op.create_index('ix_identity_group_memberships_principal_id', 'identity_group_memberships', ['principal_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "identity_group_memberships" in existing_tables:
        op.drop_index('ix_identity_group_memberships_principal_id', table_name='identity_group_memberships')
        op.drop_table('identity_group_memberships')
    if "identity_principals" in existing_tables:
        op.drop_index('ix_identity_principals_username', table_name='identity_principals')
        op.drop_index('ix_identity_principals_email', table_name='identity_principals')
        op.drop_table('identity_principals')
