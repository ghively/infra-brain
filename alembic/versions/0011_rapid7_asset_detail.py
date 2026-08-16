"""Rapid7 deep asset enrichment + remediation fix-steps (MR7).

Revision ID: 0011_rapid7_asset_detail
Revises: 0010_rapid7_relational
Create Date: 2026-06-24

Promotes the large per-asset nested lists that MR6 parked in
``r7_assets.details`` JSONB into typed child tables, and adds vulnerability /
solution detail tables for remediation fix-steps:

  * r7_software          — installed software per asset (FK r7_assets.id)
  * r7_asset_config      — system/hardware spec key-values (FK r7_assets.id)
  * r7_asset_users       — local user accounts (FK r7_assets.id)
  * r7_asset_addresses   — IP/MAC pairs (FK r7_assets.id)
  * r7_vulnerabilities   — vuln definition, keyed on the Rapid7 SLUG (no FK)
  * r7_solutions         — remediation solution + fix steps, keyed on id (no FK)
  * r7_vuln_solutions    — slug<->solution-id link table (string ids, no FK)

Note: 0001 seeds a fresh DB via ``Base.metadata.create_all()`` against the live
ORM, so on a brand-new install these tables already exist when this migration
runs. Every create is therefore guarded with the inspector (mirrors 0005-0010).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

revision = "0011_rapid7_asset_detail"
down_revision = "0010_rapid7_relational"
branch_labels = None
depends_on = None

# Cross-dialect JSON: JSONB on PostgreSQL, JSON elsewhere (mirrors models.py).
_JSONB = sa.JSON().with_variant(_PG_JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "r7_software" not in tables:
        op.create_table(
            "r7_software",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("r7_assets.id"), nullable=False),
            sa.Column("r7_asset_id", sa.Integer(), nullable=False),
            sa.Column("product", sa.String(256), nullable=False),
            sa.Column("vendor", sa.String(128), nullable=True),
            sa.Column("version", sa.String(128), nullable=True),
            sa.Column("software_type", sa.String(64), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("asset_id", "product", "version", name="uq_r7_software_natkey"),
        )
        op.create_index("ix_r7_software_asset_id", "r7_software", ["asset_id"])
        op.create_index("ix_r7_software_r7_asset_id", "r7_software", ["r7_asset_id"])

    if "r7_asset_config" not in tables:
        op.create_table(
            "r7_asset_config",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("r7_assets.id"), nullable=False),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.UniqueConstraint("asset_id", "name", name="uq_r7_asset_config_natkey"),
        )
        op.create_index("ix_r7_asset_config_asset_id", "r7_asset_config", ["asset_id"])

    if "r7_asset_users" not in tables:
        op.create_table(
            "r7_asset_users",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("r7_assets.id"), nullable=False),
            sa.Column("username", sa.String(256), nullable=False),
            sa.Column("full_name", sa.String(256), nullable=True),
            sa.UniqueConstraint("asset_id", "username", name="uq_r7_asset_user_natkey"),
        )
        op.create_index("ix_r7_asset_users_asset_id", "r7_asset_users", ["asset_id"])

    if "r7_asset_addresses" not in tables:
        op.create_table(
            "r7_asset_addresses",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("r7_assets.id"), nullable=False),
            sa.Column("ip", sa.String(64), nullable=False),
            sa.Column("mac", sa.String(64), nullable=True),
            sa.UniqueConstraint("asset_id", "ip", name="uq_r7_asset_address_natkey"),
        )
        op.create_index("ix_r7_asset_addresses_asset_id", "r7_asset_addresses", ["asset_id"])

    if "r7_vulnerabilities" not in tables:
        op.create_table(
            "r7_vulnerabilities",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_vuln_id", sa.String(128), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("severity", sa.String(16), nullable=True),
            sa.Column("cvss_v3_score", sa.Float(), nullable=True),
            sa.Column("cvss_v3_vector", sa.String(256), nullable=True),
            sa.Column("cvss_v2_score", sa.Float(), nullable=True),
            sa.Column("risk_score", sa.Float(), nullable=True),
            sa.Column("exploits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("malware_kits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("denial_of_service", sa.Boolean(), nullable=True),
            sa.Column("fix_available", sa.Boolean(), nullable=True),
            sa.Column("pci_status", sa.String(16), nullable=True),
            sa.Column("pci_fail", sa.Boolean(), nullable=True),
            sa.Column("published", sa.DateTime(timezone=True), nullable=True),
            sa.Column("categories", _JSONB, nullable=True),
            sa.Column("cves", _JSONB, nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("r7_vuln_id", name="uq_r7_vulnerability_natkey"),
        )
        op.create_index(
            "ix_r7_vulnerabilities_r7_vuln_id",
            "r7_vulnerabilities",
            ["r7_vuln_id"],
            unique=True,
        )

    if "r7_solutions" not in tables:
        op.create_table(
            "r7_solutions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_solution_id", sa.String(128), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("steps", sa.Text(), nullable=True),
            sa.Column("solution_type", sa.String(64), nullable=True),
            sa.Column("estimate", sa.String(64), nullable=True),
            sa.Column("fix_available", sa.Boolean(), nullable=True),
            sa.Column("details", _JSONB, nullable=True),
            sa.UniqueConstraint("r7_solution_id", name="uq_r7_solution_natkey"),
        )
        op.create_index(
            "ix_r7_solutions_r7_solution_id",
            "r7_solutions",
            ["r7_solution_id"],
            unique=True,
        )

    if "r7_vuln_solutions" not in tables:
        op.create_table(
            "r7_vuln_solutions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_vuln_id", sa.String(128), nullable=False),
            sa.Column("r7_solution_id", sa.String(128), nullable=False),
            sa.UniqueConstraint("r7_vuln_id", "r7_solution_id", name="uq_r7_vuln_solution_natkey"),
        )
        op.create_index("ix_r7_vuln_solutions_r7_vuln_id", "r7_vuln_solutions", ["r7_vuln_id"])
        op.create_index(
            "ix_r7_vuln_solutions_r7_solution_id", "r7_vuln_solutions", ["r7_solution_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    # Drop links/children first; the standalone vuln/solution tables have no FK
    # but are dropped after the link table for symmetry with create order.
    for tbl in (
        "r7_vuln_solutions",
        "r7_solutions",
        "r7_vulnerabilities",
        "r7_asset_addresses",
        "r7_asset_users",
        "r7_asset_config",
        "r7_software",
    ):
        if tbl in tables:
            op.drop_table(tbl)
