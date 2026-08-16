"""Repair r7_vulnerabilities / r7_solutions resource_id drift.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-29

Migration 0018 (a1b2c3d4e5f6) added the nullable ``resource_id`` FK column +
index to ``r7_vulnerabilities`` and ``r7_solutions``. On the live DEV stack that
ALTER never reached the database: the migrate step historically ran against a
divergent / throwaway DB while the live volume stayed unmigrated (the exact
failure mode alembic/env.py was later hardened against by binding the migration
URL unconditionally to ``get_settings().postgres_url``). The live
``alembic_version`` table was nonetheless advanced to (or past) 0018, so a plain
``alembic upgrade head`` is a NO-OP for these tables and never re-applies the
ADD COLUMN — leaving the ORM declaring ``R7Vulnerability.resource_id`` against a
table that has no such column.

Symptom (production /api/dashboard/vulnerabilities):

    sqlalchemy.exc.ProgrammingError: (psycopg2.errors.UndefinedColumn)
    column r7_vulnerabilities.resource_id does not exist

This migration is a fresh head revision, so it runs regardless of whatever
revision the live ``alembic_version`` currently records. It re-applies 0018's
column / FK / index additions, each guarded by an inspector existence check so
it is fully idempotent — a no-op on any DB where 0018 *did* land, and the repair
on the drifted live DB. Mirrors the 0015_repair_alter_table_ddl pattern.

``resource_id`` is legitimately part of the model: it is the nullable FK back to
the canonical ``resources`` row, populated by VulnAgent during the detail-write
phase so r7 vulns/solutions participate as knowledge-graph nodes.

Safety checklist:
  ✅ All columns nullable — no NOT NULL added to existing tables
  ✅ No DROP TABLE / DROP COLUMN on existing tables in upgrade()
  ✅ Every add guarded by an inspector existence check (idempotent / re-runnable)
  ✅ ondelete="SET NULL" — deleting a Resource never cascades a vuln/solution delete
  ✅ Downgrade is a no-op (0018 owns the drop; avoids double-drop in the chain)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID as _PG_UUID

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

# Cross-dialect UUID: PostgreSQL UUID on the live DB, CHAR(32) elsewhere — mirrors
# the resources.id PK type so the FK column matches (0010 uses sa.Uuid()).
_UUID = sa.Uuid().with_variant(_PG_UUID(as_uuid=True), "postgresql")


def _repair_resource_id(inspector, table: str, fk_name: str, ix_name: str) -> None:
    """Idempotently add a nullable resource_id FK + index to ``table``.

    Each step is guarded by an existence check so the migration is safe to run
    against a DB where 0018 already landed (no-op) and against the drifted live
    DB (applies the missing DDL). The standalone ADD CONSTRAINT (foreign key) is
    skipped on SQLite, which does not support ALTER TABLE ... ADD CONSTRAINT
    outside batch/recreate mode (the live target is PostgreSQL; the SQLite path
    exists only for the round-trip test, where an unenforced FK adds no value).
    """
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    cols = {c["name"] for c in inspector.get_columns(table)}
    if "resource_id" not in cols:
        op.add_column(table, sa.Column("resource_id", _UUID, nullable=True))

    if not is_sqlite:
        fks = {fk["name"] for fk in inspector.get_foreign_keys(table)}
        if fk_name not in fks:
            op.create_foreign_key(
                fk_name,
                table,
                "resources",
                ["resource_id"],
                ["id"],
                ondelete="SET NULL",
            )

    idxs = {idx["name"] for idx in inspector.get_indexes(table)}
    if ix_name not in idxs:
        op.create_index(ix_name, table, ["resource_id"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "r7_vulnerabilities" in tables:
        _repair_resource_id(
            inspector,
            "r7_vulnerabilities",
            "fk_r7_vuln_resource",
            "ix_r7_vulnerabilities_resource_id",
        )

    if "r7_solutions" in tables:
        _repair_resource_id(
            inspector,
            "r7_solutions",
            "fk_r7_solution_resource",
            "ix_r7_solutions_resource_id",
        )


def downgrade() -> None:
    # 0018 (a1b2c3d4e5f6) owns the authoritative drop of these columns. This
    # repair revision intentionally does not drop them on downgrade to avoid a
    # double-drop when 0018 is also present in the chain.
    pass
