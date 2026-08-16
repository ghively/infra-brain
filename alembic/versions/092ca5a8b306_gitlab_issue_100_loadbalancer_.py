"""GitLab issue #100: loadbalancer relational model

Revision ID: 092ca5a8b306
Revises: 9d83fb71e295
Create Date: 2026-07-29 21:22:03.659904

Adds LbInstance/LbPool/LbPoolMember/LbVirtualServer (load balancer / reverse
proxy / CDN collection — F5/nginx Plus/HAProxy/Cloudflare).

Note: 0002/0003/0014/0026 seed a fresh DB via ``Base.metadata.create_all()``
against the LIVE ORM (not a frozen snapshot), so on a brand-new install these
tables already exist by the time this migration runs. Every create is
therefore guarded with the inspector — mirrors 0009_cloud_k8s_net_relational's
established pattern for the exact same reason.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "092ca5a8b306"
down_revision: Union[str, Sequence[str], None] = "9d83fb71e295"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "lb_instances" not in tables:
        op.create_table(
            "lb_instances",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("resource_id", sa.Uuid(), nullable=True),
            sa.Column("vendor", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column("management_host", sa.String(length=256), nullable=True),
            sa.Column("version", sa.String(length=64), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.ForeignKeyConstraint(
                ["resource_id"],
                ["resources.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("vendor", "name", name="uq_lb_instance_natkey"),
        )
        op.create_index("ix_lb_instances_natkey", "lb_instances", ["vendor", "name"], unique=False)

    if "lb_pools" not in tables:
        op.create_table(
            "lb_pools",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("resource_id", sa.Uuid(), nullable=True),
            sa.Column("instance_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column("monitor_type", sa.String(length=64), nullable=True),
            sa.Column("monitor_interval_seconds", sa.Integer(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.ForeignKeyConstraint(["instance_id"], ["lb_instances.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["resource_id"],
                ["resources.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("instance_id", "name", name="uq_lb_pool_natkey"),
        )
        op.create_index("ix_lb_pools_natkey", "lb_pools", ["instance_id", "name"], unique=False)

    if "lb_pool_members" not in tables:
        op.create_table(
            "lb_pool_members",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("resource_id", sa.Uuid(), nullable=True),
            sa.Column("pool_id", sa.Uuid(), nullable=False),
            sa.Column("address", sa.String(length=256), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=True),
            sa.Column("weight", sa.Integer(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.ForeignKeyConstraint(["pool_id"], ["lb_pools.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["resource_id"],
                ["resources.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("pool_id", "address", "port", name="uq_lb_pool_member_natkey"),
        )
        op.create_index(
            "ix_lb_pool_members_natkey",
            "lb_pool_members",
            ["pool_id", "address", "port"],
            unique=False,
        )

    if "lb_virtual_servers" not in tables:
        op.create_table(
            "lb_virtual_servers",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("resource_id", sa.Uuid(), nullable=True),
            sa.Column("instance_id", sa.Uuid(), nullable=False),
            sa.Column("pool_id", sa.Uuid(), nullable=True),
            sa.Column("name", sa.String(length=256), nullable=False),
            sa.Column("address", sa.String(length=256), nullable=True),
            sa.Column("port", sa.Integer(), nullable=True),
            sa.Column("protocol", sa.String(length=32), nullable=True),
            sa.Column("tls_enabled", sa.Boolean(), nullable=False),
            sa.Column("tls_certificate_thumbprint", sa.String(length=128), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.ForeignKeyConstraint(["instance_id"], ["lb_instances.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["pool_id"], ["lb_pools.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["resource_id"],
                ["resources.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("instance_id", "name", name="uq_lb_virtual_server_natkey"),
        )
        op.create_index(
            "ix_lb_virtual_servers_natkey",
            "lb_virtual_servers",
            ["instance_id", "name"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for tbl in ("lb_virtual_servers", "lb_pool_members", "lb_pools", "lb_instances"):
        if tbl in tables:
            op.drop_table(tbl)
