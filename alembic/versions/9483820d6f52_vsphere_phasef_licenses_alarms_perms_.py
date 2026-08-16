"""vsphere_phaseF_licenses_alarms_perms_sessions

Revision ID: 9483820d6f52
Revises: 65f1043a2504
Create Date: 2026-07-13 18:30:04.192842

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9483820d6f52"
down_revision: Union[str, Sequence[str], None] = "65f1043a2504"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guards every CREATE TABLE + its CREATE INDEX with an existence check so
    that the migration is idempotent on a database where Base.metadata.create_all()
    was called before this migration was authored (persistent CI runner scenario).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "vsphere_alarms" not in tables:
        op.create_table(
            "vsphere_alarms",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("alarm_name", sa.String(length=256), nullable=True),
            sa.Column("entity_name", sa.String(length=256), nullable=True),
            sa.Column("entity_type", sa.String(length=64), nullable=True),
            sa.Column("overall_status", sa.String(length=16), nullable=True),
            sa.Column("acknowledged", sa.Boolean(), nullable=True),
            sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_vsphere_alarms_vcenter"), "vsphere_alarms", ["vcenter"], unique=False
        )

    if "vsphere_licenses" not in tables:
        op.create_table(
            "vsphere_licenses",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=True),
            sa.Column("edition_key", sa.String(length=128), nullable=True),
            sa.Column("total", sa.Integer(), nullable=True),
            sa.Column("used", sa.Integer(), nullable=True),
            sa.Column("expiration", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "details",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_vsphere_licenses_vcenter"), "vsphere_licenses", ["vcenter"], unique=False
        )

    if "vsphere_permissions" not in tables:
        op.create_table(
            "vsphere_permissions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("principal", sa.String(length=256), nullable=True),
            sa.Column("role_id", sa.Integer(), nullable=True),
            sa.Column("role_name", sa.String(length=256), nullable=True),
            sa.Column("is_group", sa.Boolean(), nullable=True),
            sa.Column("propagate", sa.Boolean(), nullable=True),
            sa.Column("entity", sa.String(length=256), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_vsphere_permissions_vcenter"), "vsphere_permissions", ["vcenter"], unique=False
        )

    if "vsphere_sessions" not in tables:
        op.create_table(
            "vsphere_sessions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("vcenter", sa.String(length=256), nullable=False),
            sa.Column("user_name", sa.String(length=256), nullable=True),
            sa.Column("full_name", sa.String(length=256), nullable=True),
            sa.Column("login_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_active", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=256), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_vsphere_sessions_vcenter"), "vsphere_sessions", ["vcenter"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_vsphere_sessions_vcenter"), table_name="vsphere_sessions")
    op.drop_table("vsphere_sessions")
    op.drop_index(op.f("ix_vsphere_permissions_vcenter"), table_name="vsphere_permissions")
    op.drop_table("vsphere_permissions")
    op.drop_index(op.f("ix_vsphere_licenses_vcenter"), table_name="vsphere_licenses")
    op.drop_table("vsphere_licenses")
    op.drop_index(op.f("ix_vsphere_alarms_vcenter"), table_name="vsphere_alarms")
    op.drop_table("vsphere_alarms")
