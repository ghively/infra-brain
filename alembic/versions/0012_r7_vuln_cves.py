"""Rapid7 vuln-slug <-> CVE bridge (MR8).

Revision ID: 0012_r7_vuln_cves
Revises: 0011_rapid7_asset_detail
Create Date: 2026-06-24

Adds one new table:

  * r7_vuln_cves — maps a Rapid7 vulnerability SLUG (``r7_vuln_id``) to each
    canonical ``CVE-YYYY-N`` it covers (string ids, no FK), so ``vuln_queue``
    (keyed by CVE) can join through to ``r7_vulnerabilities`` (CVSS / exploits /
    PCI) and ``r7_vuln_solutions`` -> ``r7_solutions`` (fix steps):
    ``vuln_queue.cve_id -> r7_vuln_cves.cve_id -> r7_vuln_id ->
    r7_vulnerabilities``.

A NEW table is used deliberately rather than an ALTER on vuln_queue: brand-new
tables appear on a fresh DB via 0001's ``Base.metadata.create_all()`` against
the live ORM, so the create here is inspector-guarded (mirrors 0005-0011).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0012_r7_vuln_cves"
down_revision = "0011_rapid7_asset_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "r7_vuln_cves" not in tables:
        op.create_table(
            "r7_vuln_cves",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("r7_vuln_id", sa.String(128), nullable=False),
            sa.Column("cve_id", sa.String(32), nullable=False),
            sa.UniqueConstraint("r7_vuln_id", "cve_id", name="uq_r7_vuln_cve_natkey"),
        )
        op.create_index("ix_r7_vuln_cves_r7_vuln_id", "r7_vuln_cves", ["r7_vuln_id"])
        op.create_index("ix_r7_vuln_cves_cve_id", "r7_vuln_cves", ["cve_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "r7_vuln_cves" in tables:
        op.drop_table("r7_vuln_cves")
