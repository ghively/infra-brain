"""Rapid7 InsightVM relational model (MR6).

Revision ID: 0010_rapid7_relational
Revises: 0009_cloud_k8s_net_relational
Create Date: 2026-06-24

Gives the Rapid7 vuln collector dedicated relational tables instead of a thin
generic Resource + JSONB (and fixes the OS capture bug — the list-item ``os`` is
a STRING, not a dict). Each table keys on the upstream Rapid7 integer id:

  * r7_assets  — one InsightVM asset, key ``r7_asset_id``. Typed OS (string +
                 broken-out osFingerprint fields), risk scores, the six Rapid7
                 vuln-band counts (critical/severe/moderate — NO "high"), with
                 the big nested lists (software/users/configurations/addresses/
                 ids/hostNames) in a JSONB ``details`` overflow. Nullable
                 ``resource_id`` FK back to ``resources``.
  * r7_sites   — InsightVM site, key ``r7_site_id``.
  * r7_tags    — InsightVM tag, key ``r7_tag_id``.

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install these tables already exist when this migration
runs. Every create is therefore guarded with the inspector (mirrors 0005-0009).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0010_rapid7_relational"
down_revision = "0009_cloud_k8s_net_relational"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "r7_assets" not in tables:
        op.create_table(
            "r7_assets",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("resource_id", sa.Uuid(), sa.ForeignKey("resources.id"), nullable=True),
            sa.Column("r7_asset_id", sa.Integer(), nullable=False),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("hostname", sa.String(256), nullable=True),
            sa.Column("mac", sa.String(64), nullable=True),
            sa.Column("os", sa.String(256), nullable=True),
            sa.Column("os_family", sa.String(64), nullable=True),
            sa.Column("os_product", sa.String(256), nullable=True),
            sa.Column("os_version", sa.String(64), nullable=True),
            sa.Column("os_vendor", sa.String(128), nullable=True),
            sa.Column("os_architecture", sa.String(32), nullable=True),
            sa.Column("asset_type", sa.String(32), nullable=True),
            sa.Column("risk_score", sa.Float(), nullable=True),
            sa.Column("raw_risk_score", sa.Float(), nullable=True),
            sa.Column("vuln_critical", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("vuln_severe", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("vuln_moderate", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("vuln_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("vuln_exploits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("vuln_malware_kits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "assessed_for_vulnerabilities",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("r7_asset_id", name="uq_r7_asset_natkey"),
        )
        op.create_index("ix_r7_assets_r7_asset_id", "r7_assets", ["r7_asset_id"], unique=True)

    if "r7_sites" not in tables:
        op.create_table(
            "r7_sites",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_site_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("asset_count", sa.Integer(), nullable=True),
            sa.Column("risk_score", sa.Float(), nullable=True),
            sa.Column("importance", sa.String(32), nullable=True),
            sa.Column("site_type", sa.String(32), nullable=True),
            sa.Column("last_scan_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("r7_site_id", name="uq_r7_site_natkey"),
        )
        op.create_index("ix_r7_sites_r7_site_id", "r7_sites", ["r7_site_id"], unique=True)

    if "r7_tags" not in tables:
        op.create_table(
            "r7_tags",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_tag_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("tag_type", sa.String(32), nullable=True),
            sa.Column("color", sa.String(32), nullable=True),
            sa.Column("source", sa.String(64), nullable=True),
            sa.UniqueConstraint("r7_tag_id", name="uq_r7_tag_natkey"),
        )
        op.create_index("ix_r7_tags_r7_tag_id", "r7_tags", ["r7_tag_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for tbl in ("r7_tags", "r7_sites", "r7_assets"):
        if tbl in tables:
            op.drop_table(tbl)
