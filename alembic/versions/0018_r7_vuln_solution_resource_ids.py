"""r7_vuln_solution_resource_ids — link r7_vulnerabilities and r7_solutions to
the canonical Resource table so they participate as proper knowledge-graph nodes.

Revision ID: a1b2c3d4e5f6
Revises: 8f3a2b1c9d4e
Create Date: 2026-06-28

Adds nullable ``resource_id`` FK columns + indexes to:

  * r7_vulnerabilities.resource_id → resources.id  (SET NULL on delete)
  * r7_solutions.resource_id       → resources.id  (SET NULL on delete)

These columns are populated by VulnAgent._write_vuln_details() during the
detail-write phase (every collection run), which upserts one Resource row per
distinct vuln/solution SLUG and writes the id back here.  They are nullable
because existing rows populated before this migration have no Resource counterpart
yet — the next collection run fills them in.

Safety checklist:
  ✅ All columns are nullable — no NOT NULL added to existing tables
  ✅ No DROP TABLE or DROP COLUMN on existing tables
  ✅ No CONCURRENTLY concern — small tables (cap 600 vulns / solutions per run)
  ✅ Downgrade present and idempotent (inspect guard on constraints + indexes)
  ✅ ondelete="SET NULL" so deleting a Resource row never cascades a vuln delete
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "8f3a2b1c9d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── r7_vulnerabilities ────────────────────────────────────────────────────
    vuln_cols = {c["name"] for c in inspector.get_columns("r7_vulnerabilities")}
    if "resource_id" not in vuln_cols:
        op.add_column(
            "r7_vulnerabilities",
            sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    vuln_fks = {fk["name"] for fk in inspector.get_foreign_keys("r7_vulnerabilities")}
    if "fk_r7_vuln_resource" not in vuln_fks:
        op.create_foreign_key(
            "fk_r7_vuln_resource",
            "r7_vulnerabilities",
            "resources",
            ["resource_id"],
            ["id"],
            ondelete="SET NULL",
        )
    vuln_idxs = {idx["name"] for idx in inspector.get_indexes("r7_vulnerabilities")}
    if "ix_r7_vulnerabilities_resource_id" not in vuln_idxs:
        op.create_index(
            "ix_r7_vulnerabilities_resource_id",
            "r7_vulnerabilities",
            ["resource_id"],
        )

    # ── r7_solutions ──────────────────────────────────────────────────────────
    sol_cols = {c["name"] for c in inspector.get_columns("r7_solutions")}
    if "resource_id" not in sol_cols:
        op.add_column(
            "r7_solutions",
            sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    sol_fks = {fk["name"] for fk in inspector.get_foreign_keys("r7_solutions")}
    if "fk_r7_solution_resource" not in sol_fks:
        op.create_foreign_key(
            "fk_r7_solution_resource",
            "r7_solutions",
            "resources",
            ["resource_id"],
            ["id"],
            ondelete="SET NULL",
        )
    sol_idxs = {idx["name"] for idx in inspector.get_indexes("r7_solutions")}
    if "ix_r7_solutions_resource_id" not in sol_idxs:
        op.create_index(
            "ix_r7_solutions_resource_id",
            "r7_solutions",
            ["resource_id"],
        )


def downgrade() -> None:
    # ── r7_solutions ──────────────────────────────────────────────────────────
    inspector = sa.inspect(op.get_bind())
    sol_fks = {fk["name"] for fk in inspector.get_foreign_keys("r7_solutions")}
    if "fk_r7_solution_resource" in sol_fks:
        op.drop_constraint("fk_r7_solution_resource", "r7_solutions", type_="foreignkey")
    sol_idxs = {idx["name"] for idx in inspector.get_indexes("r7_solutions")}
    if "ix_r7_solutions_resource_id" in sol_idxs:
        op.drop_index("ix_r7_solutions_resource_id", table_name="r7_solutions")
    sol_cols = {col["name"] for col in inspector.get_columns("r7_solutions")}
    if "resource_id" in sol_cols:
        op.drop_column("r7_solutions", "resource_id")

    # ── r7_vulnerabilities ────────────────────────────────────────────────────
    vuln_fks = {fk["name"] for fk in inspector.get_foreign_keys("r7_vulnerabilities")}
    if "fk_r7_vuln_resource" in vuln_fks:
        op.drop_constraint("fk_r7_vuln_resource", "r7_vulnerabilities", type_="foreignkey")
    vuln_idxs = {idx["name"] for idx in inspector.get_indexes("r7_vulnerabilities")}
    if "ix_r7_vulnerabilities_resource_id" in vuln_idxs:
        op.drop_index("ix_r7_vulnerabilities_resource_id", table_name="r7_vulnerabilities")
    vuln_cols = {col["name"] for col in inspector.get_columns("r7_vulnerabilities")}
    if "resource_id" in vuln_cols:
        op.drop_column("r7_vulnerabilities", "resource_id")
