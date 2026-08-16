"""Cloud / Kubernetes / Network relational model (MR5).

Revision ID: 0009_cloud_k8s_net_relational
Revises: 0008_vuln_eol_writer_indexes
Create Date: 2026-06-24

Gives the cloud, k8s, and net collectors dedicated relational tables instead of
flat Resource+JSONB. Each table carries typed columns + a JSONB ``details``
overflow and a nullable ``resource_id`` FK back to ``resources``, keyed on its
natural key (the same upsert pattern as the octopus / iac / vsphere models):

  * cloud_resources   — one AWS resource (ec2_instance / vpc / security_group),
                        key (provider, cloud_id).
  * k8s_nodes         — key (cluster, name). cluster nullable (single in-context).
  * k8s_pods          — key (cluster, namespace, name).
  * k8s_deployments   — key (cluster, namespace, name).
  * net_devices       — SNMP-discovered device, key (ip = snmp target).

All three collectors are idle (AWS off / no clusters / SNMP empty), so these
ship "ready for when enabled" with no live data.

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install these tables already exist when this migration
runs. Every create is therefore guarded with the inspector (mirrors 0005-0008).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0009_cloud_k8s_net_relational"
down_revision = "0008_vuln_eol_writer_indexes"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "cloud_resources" not in tables:
        op.create_table(
            "cloud_resources",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("cloud_type", sa.String(64), nullable=False),
            sa.Column("cloud_id", sa.String(256), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("region", sa.String(64), nullable=True),
            sa.Column("state", sa.String(64), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("provider", "cloud_id", name="uq_cloud_resource_natkey"),
        )
        op.create_index("ix_cloud_resources_natkey", "cloud_resources", ["provider", "cloud_id"])

    if "k8s_nodes" not in tables:
        op.create_table(
            "k8s_nodes",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("cluster", sa.String(256), nullable=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("roles", _JSONB, nullable=True),
            sa.Column("kubelet_version", sa.String(64), nullable=True),
            sa.Column("arch", sa.String(32), nullable=True),
            sa.Column("os_image", sa.String(256), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("cluster", "name", name="uq_k8s_node_natkey"),
        )
        op.create_index("ix_k8s_nodes_natkey", "k8s_nodes", ["cluster", "name"])

    if "k8s_pods" not in tables:
        op.create_table(
            "k8s_pods",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("cluster", sa.String(256), nullable=True),
            sa.Column("namespace", sa.String(256), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("phase", sa.String(32), nullable=True),
            sa.Column("node_name", sa.String(256), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("cluster", "namespace", "name", name="uq_k8s_pod_natkey"),
        )
        op.create_index("ix_k8s_pods_natkey", "k8s_pods", ["cluster", "namespace", "name"])

    if "k8s_deployments" not in tables:
        op.create_table(
            "k8s_deployments",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("cluster", sa.String(256), nullable=True),
            sa.Column("namespace", sa.String(256), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("replicas", sa.Integer(), nullable=True),
            sa.Column("ready", sa.Integer(), nullable=True),
            sa.Column("available", sa.Integer(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("cluster", "namespace", "name", name="uq_k8s_deployment_natkey"),
        )
        op.create_index(
            "ix_k8s_deployments_natkey", "k8s_deployments", ["cluster", "namespace", "name"]
        )

    if "net_devices" not in tables:
        op.create_table(
            "net_devices",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("ip", sa.String(128), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("sysname", sa.String(256), nullable=True),
            sa.Column("descr", sa.Text(), nullable=True),
            sa.Column("object_id", sa.String(256), nullable=True),
            sa.Column("uptime", sa.String(128), nullable=True),
            sa.Column("contact", sa.String(256), nullable=True),
            sa.Column("location", sa.String(256), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("ip", name="uq_net_device_natkey"),
        )
        op.create_index("ix_net_devices_natkey", "net_devices", ["ip"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for tbl in (
        "net_devices",
        "k8s_deployments",
        "k8s_pods",
        "k8s_nodes",
        "cloud_resources",
    ):
        if tbl in tables:
            op.drop_table(tbl)
