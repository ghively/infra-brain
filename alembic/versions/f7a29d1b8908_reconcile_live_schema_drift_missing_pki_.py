"""reconcile live schema drift: missing pki/container_registry tables plus 46 server_default mismatches

Revision ID: f7a29d1b8908
Revises: cda109223f3c
Create Date: 2026-07-30 11:58:22.074260

TRK-292 (2026-07-30 incident): the live deploy-host-01 DB was missing
certificate_authorities/container_images entirely (GitLab #94/#101 migrations
wrongly assumed "covered by the 0001_initial_schema catch-all"; true only for
a brand-new DB, not this already-migrated one), plus 46 server_default
mismatches across 16 other tables that were never caught by CI because
alembic/env.py lacked compare_server_default=True (fixed separately, same
branch). Generated via `alembic revision --autogenerate` against a schema-only
pg_dump of the real live DB loaded into an ephemeral replica and stamped at
the live revision — not against a fresh/reset DB, which is what let this drift
go undetected by /pg-gate-check for as long as it did.

Both new-table CREATE statements are wrapped in an inspector existing_tables
guard (this repo's established idempotency pattern — see
6dd56fd814ab_add_backup_jobs_table.py) because on a FRESH database 0001's
blanket Base.metadata.create_all() already creates these two tables (they're
part of Base.metadata now), so this migration's own unconditional
op.create_table raised DuplicateTable on /pg-gate-check's from-scratch chain —
caught by re-running /pg-gate-check after generating this migration, before
it ever reached production.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f7a29d1b8908'
down_revision: Union[str, Sequence[str], None] = 'cda109223f3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "certificate_authorities" not in existing_tables:
        op.create_table('certificate_authorities',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('ca_type', sa.String(length=16), nullable=False),
        sa.Column('issuer', sa.Text(), nullable=True),
        sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
        sa.Column('not_after', sa.DateTime(timezone=True), nullable=True),
        sa.Column('crl_url', sa.Text(), nullable=True),
        sa.Column('ocsp_url', sa.Text(), nullable=True),
        sa.Column('crl_status', sa.String(length=16), nullable=True),
        sa.Column('crl_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ocsp_status', sa.String(length=16), nullable=True),
        sa.Column('ocsp_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_expired', sa.Boolean(), nullable=False),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'ca_type', name='uq_certificate_authority_natkey')
        )
        op.create_index(op.f('ix_certificate_authorities_resource_id'), 'certificate_authorities', ['resource_id'], unique=False)

    if "container_images" not in existing_tables:
        op.create_table('container_images',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('resource_id', sa.Uuid(), nullable=False),
        sa.Column('registry', sa.String(length=512), nullable=False),
        sa.Column('repo', sa.String(length=512), nullable=False),
        sa.Column('tag', sa.String(length=256), nullable=False),
        sa.Column('digest', sa.String(length=128), nullable=False),
        sa.Column('scan_result_summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
        sa.Column('signed', sa.Boolean(), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('registry', 'repo', 'digest', name='uq_container_image_registry_repo_digest')
        )

    op.alter_column('collection_runs', 'detail_rows_written',
               existing_type=sa.INTEGER(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('document_chunks', 'chunk_index',
               existing_type=sa.INTEGER(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('document_chunks', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('documents', 'status',
               existing_type=sa.VARCHAR(length=32),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_certificates', 'thumbprint',
               existing_type=sa.VARCHAR(length=64),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_certificates', 'is_expired',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_certificates', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_purpose_map', 'source',
               existing_type=sa.VARCHAR(length=256),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_purpose_map', 'updated_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_security_posture', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('host_shares', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'allowed_tools',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=None,
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'created_by',
               existing_type=sa.VARCHAR(length=128),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'first_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'responded',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'discovery_tier',
               existing_type=sa.VARCHAR(length=16),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_fragile',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_known',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_shadow_it',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'threat_level',
               existing_type=sa.VARCHAR(length=8),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'is_dangerous',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'is_suspicious',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('octopus_projects', 'included_library_variable_set_ids',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=None,
               existing_nullable=False)
    op.alter_column('saas_api_key_metadata', 'id',
               existing_type=sa.UUID(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('saas_api_key_metadata', 'is_active',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('saas_applications', 'id',
               existing_type=sa.UUID(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('saas_applications', 'details',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=None,
               existing_nullable=False)
    op.alter_column('saas_applications', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'payload_summary',
               existing_type=sa.TEXT(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'status',
               existing_type=sa.VARCHAR(length=16),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'attempt_count',
               existing_type=sa.INTEGER(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'max_attempts',
               existing_type=sa.INTEGER(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'event_pattern',
               existing_type=sa.VARCHAR(length=128),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'active',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'description',
               existing_type=sa.TEXT(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'created_by',
               existing_type=sa.VARCHAR(length=128),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'updated_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('windows_local_group_members', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('windows_local_users', 'is_admin',
               existing_type=sa.BOOLEAN(),
               server_default=None,
               existing_nullable=False)
    op.alter_column('windows_local_users', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('windows_logon_events', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('windows_logon_events', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('windows_local_users', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('windows_local_users', 'is_admin',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('windows_local_group_members', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'updated_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'created_by',
               existing_type=sa.VARCHAR(length=128),
               server_default=sa.text("''::character varying"),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'description',
               existing_type=sa.TEXT(),
               server_default=sa.text("''::text"),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'active',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('true'),
               existing_nullable=False)
    op.alter_column('webhook_subscriptions', 'event_pattern',
               existing_type=sa.VARCHAR(length=128),
               server_default=sa.text("'*'::character varying"),
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'max_attempts',
               existing_type=sa.INTEGER(),
               server_default=sa.text('5'),
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'attempt_count',
               existing_type=sa.INTEGER(),
               server_default=sa.text('0'),
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'status',
               existing_type=sa.VARCHAR(length=16),
               server_default=sa.text("'pending'::character varying"),
               existing_nullable=False)
    op.alter_column('webhook_deliveries', 'payload_summary',
               existing_type=sa.TEXT(),
               server_default=sa.text("''::text"),
               existing_nullable=False)
    op.alter_column('saas_applications', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('saas_applications', 'details',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=sa.text("'{}'::jsonb"),
               existing_nullable=False)
    op.alter_column('saas_applications', 'id',
               existing_type=sa.UUID(),
               server_default=sa.text('gen_random_uuid()'),
               existing_nullable=False)
    op.alter_column('saas_api_key_metadata', 'is_active',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('true'),
               existing_nullable=False)
    op.alter_column('saas_api_key_metadata', 'id',
               existing_type=sa.UUID(),
               server_default=sa.text('gen_random_uuid()'),
               existing_nullable=False)
    op.alter_column('octopus_projects', 'included_library_variable_set_ids',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=sa.text("'[]'::jsonb"),
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'is_suspicious',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_services', 'is_dangerous',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'threat_level',
               existing_type=sa.VARCHAR(length=8),
               server_default=sa.text("'none'::character varying"),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_shadow_it',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_known',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'is_fragile',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'discovery_tier',
               existing_type=sa.VARCHAR(length=16),
               server_default=sa.text("'passive'::character varying"),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'responded',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'last_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('net_discovery_hosts', 'first_seen',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'created_by',
               existing_type=sa.VARCHAR(length=128),
               server_default=sa.text("''::character varying"),
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('mcp_api_keys', 'allowed_tools',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               server_default=sa.text("'[]'::jsonb"),
               existing_nullable=False)
    op.alter_column('host_shares', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('host_security_posture', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('host_purpose_map', 'updated_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('host_purpose_map', 'source',
               existing_type=sa.VARCHAR(length=256),
               server_default=sa.text("''::character varying"),
               existing_nullable=False)
    op.alter_column('host_certificates', 'collected_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('host_certificates', 'is_expired',
               existing_type=sa.BOOLEAN(),
               server_default=sa.text('false'),
               existing_nullable=False)
    op.alter_column('host_certificates', 'thumbprint',
               existing_type=sa.VARCHAR(length=64),
               server_default=sa.text("''::character varying"),
               existing_nullable=False)
    op.alter_column('documents', 'status',
               existing_type=sa.VARCHAR(length=32),
               server_default=sa.text("'current'::character varying"),
               existing_nullable=False)
    op.alter_column('document_chunks', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.alter_column('document_chunks', 'chunk_index',
               existing_type=sa.INTEGER(),
               server_default=sa.text('0'),
               existing_nullable=False)
    op.alter_column('collection_runs', 'detail_rows_written',
               existing_type=sa.INTEGER(),
               server_default=sa.text('0'),
               existing_nullable=False)

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "container_images" in existing_tables:
        op.drop_table('container_images')
    if "certificate_authorities" in existing_tables:
        op.drop_index(op.f('ix_certificate_authorities_resource_id'), table_name='certificate_authorities')
        op.drop_table('certificate_authorities')
    # ### end Alembic commands ###
